"""LLM client — talks to Anthropic (Claude) OR any OpenAI-compatible endpoint.

Design note: the mentor's *intelligence* lives here (blueprint, reasoning, nudges).
The cheap deterministic detection (typos/syntax) lives in analyzer.py and does NOT
call this. This keeps the LLM reserved for genuinely open-ended teaching.

In "offline" mode the client returns deterministic, AST-informed canned responses so
the whole loop runs with zero setup — useful for wiring the extension and demos.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import List, Dict

from .config import Config, ANTHROPIC_VERSION, provider_for


class LLMClient:
    def __init__(self, config: Config):
        self.config = config

    @property
    def offline(self) -> bool:
        return self.config.offline

    def chat(self, system: str, user: str, temperature: float = 0.3,
             max_tokens: int = 800, model: str | None = None) -> str:
        """Send a single-turn chat request. Returns the assistant text.

        The provider is chosen per model name (claude* -> Anthropic, else OpenAI),
        so both can be active at once. The OpenAI path self-heals across API
        generations (adapts max_tokens/max_completion_tokens/temperature).
        Uses urllib (stdlib) — no third-party HTTP dependency.
        """
        if self.offline:
            return "(offline mode: no LLM configured)"

        model = model or self.config.model
        provider = provider_for(model)
        key, base_url = self.config.creds_for(provider)
        if not key:
            raise LLMError(
                f"Model '{model}' needs the {provider} provider, but no {provider} "
                f"API key is configured. Add it to .env, or pick a different model.",
                retryable=False)

        if provider == "anthropic":
            return _require_nonempty(
                self._chat_anthropic(base_url, key, system, user, temperature, max_tokens, model))

        params: Dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_completion_tokens": max_tokens,
        }
        for _ in range(4):
            try:
                return _require_nonempty(self._post_chat(base_url, key, params))
            except _ParamError as pe:
                if not pe.adjust(params):
                    # A genuine malformed/unsupported request — don't mask it via failover.
                    raise LLMError(f"LLM rejected request: {pe.detail}", retryable=False) from pe
        raise LLMError("LLM request could not be adapted to the model.", retryable=False)

    def chat_with_failover(self, system: str, user: str, models: List[str],
                           temperature: float = 0.3, max_tokens: int = 800) -> tuple[str, str]:
        """Try each model in order; if one fails (timeout, empty, error), fall back to
        the next — which is the other configured provider.

        Returns (text, model_used) so callers can show which provider actually answered.
        """
        if self.offline:
            return "(offline mode: no LLM configured)", "offline"
        last: Exception | None = None
        for m in models:
            try:
                return self.chat(system, user, temperature, max_tokens, model=m), m
            except LLMError as e:
                last = e
                # Only fall over on transient failures. A hard error (bad key,
                # unsupported model, malformed request, refusal) surfaces immediately
                # so it isn't masked by silently switching providers.
                if not e.retryable:
                    raise
                continue
        raise last or LLMError("No model available to answer.")

    # --- Anthropic (Claude) Messages API ---------------------------------
    def _chat_anthropic(self, base_url: str, key: str, system: str, user: str,
                        temperature: float, max_tokens: int, model: str) -> str:
        # Prompt caching: the system prompt is identical across many calls in a
        # session, so mark it cacheable. Cache reads cost ~1/10th of input.
        # (Caching only engages above the provider's minimum token threshold; below
        # it, this is a harmless no-op.)
        if self.config.prompt_cache:
            system_field = [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }]
        else:
            system_field = system

        payload = {
            "model": model,
            "max_tokens": max_tokens,          # required by Anthropic
            "system": system_field,            # top-level, not a message
            "temperature": temperature,
            "messages": [{"role": "user", "content": user}],
        }
        # Self-heal: some newer Claude models deprecate `temperature`. On a 400 that
        # names an unsupported field, drop/adjust it and retry (mirrors the OpenAI path).
        for _ in range(3):
            try:
                return self._post_anthropic(base_url, key, payload)
            except _ParamError as pe:
                if not _adjust_anthropic(payload, pe.detail):
                    raise LLMError(f"Claude rejected request: {pe.detail}", retryable=False) from pe
        raise LLMError("Claude request could not be adapted to the model.", retryable=False)

    def _post_anthropic(self, base_url: str, key: str, payload: Dict) -> str:
        req = urllib.request.Request(
            url=f"{base_url}/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-api-key": key,
                "anthropic-version": ANTHROPIC_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.config.request_timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                text = "".join(b.get("text", "") for b in body.get("content", [])
                               if b.get("type") == "text").strip()
                if not text:
                    # Diagnose WHY it's empty rather than showing a blank bubble.
                    stop = body.get("stop_reason", "unknown")
                    if stop == "max_tokens":
                        raise LLMError("Claude hit the token limit before producing an "
                                       "answer — raising max_tokens should fix this.")
                    # A safety refusal is not transient — don't fail over (the other
                    # provider will likely refuse too, and it's not a failure to mask).
                    refusal = stop in ("refusal", "stop_sequence") or "refus" in str(stop).lower()
                    raise LLMError(f"Claude returned no text (stop_reason={stop}).",
                                   retryable=not refusal)
                return text
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            if e.code == 400:
                raise _ParamError(detail) from e
            raise _http_error("Claude", e.code, detail) from e
        except urllib.error.URLError as e:
            raise LLMError(f"Claude connection failed: {e.reason}") from e
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise LLMError(f"Unexpected Claude response shape: {e}") from e

    def list_models(self) -> List[str]:
        """Combined model IDs across ALL configured providers. Sorted, de-duped."""
        out: List[str] = []
        for provider in self.config.available_providers():
            try:
                out += self._list_provider(provider)
            except LLMError:
                pass  # one provider being unreachable shouldn't hide the other
        return sorted(set(out))

    def _list_provider(self, provider: str) -> List[str]:
        key, base_url = self.config.creds_for(provider)
        if provider == "anthropic":
            url = f"{base_url}/v1/models"
            headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
        else:
            url = f"{base_url}/models"
            headers = {"Authorization": f"Bearer {key}"}
        req = urllib.request.Request(url=url, method="GET", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.config.request_timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return [m["id"] for m in body.get("data", [])]
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise LLMError(f"{provider} HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise LLMError(f"{provider} connection failed: {e.reason}") from e
        except (KeyError, json.JSONDecodeError) as e:
            raise LLMError(f"Unexpected /models response: {e}") from e

    def _post_chat(self, base_url: str, key: str, params: Dict) -> str:
        req = urllib.request.Request(
            url=f"{base_url}/chat/completions",
            data=json.dumps(params).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.config.request_timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return body["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            if e.code == 400:
                raise _ParamError(detail) from e
            raise _http_error("LLM", e.code, detail) from e
        except urllib.error.URLError as e:
            raise LLMError(f"LLM connection failed: {e.reason}") from e
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise LLMError(f"Unexpected LLM response shape: {e}") from e


def _adjust_anthropic(payload: Dict, detail: str) -> bool:
    """Work around a rejected Anthropic parameter. Returns True if changed."""
    d = detail.lower()
    # Newer models deprecate `temperature`; just drop it.
    if "temperature" in d and "temperature" in payload:
        payload.pop("temperature")
        return True
    if "top_p" in d and "top_p" in payload:
        payload.pop("top_p")
        return True
    return False


def _require_nonempty(text: str) -> str:
    """Turn a blank model response into a clear error instead of a silent empty bubble."""
    if text and text.strip():
        return text
    raise LLMError("The model returned an empty response. Try again or rephrase.")


class _ParamError(Exception):
    """A 400 that likely names an unsupported parameter we can adapt around."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail

    def adjust(self, params: Dict) -> bool:
        """Mutate params to work around the complaint. Returns True if we changed something."""
        d = self.detail.lower()
        # Model wants the legacy token field.
        if "max_completion_tokens" in d and "max_completion_tokens" in params:
            params["max_tokens"] = params.pop("max_completion_tokens")
            return True
        # Model wants the newer token field.
        if "max_tokens" in d and "max_tokens" in params:
            params["max_completion_tokens"] = params.pop("max_tokens")
            return True
        # Model only supports the default temperature.
        if "temperature" in d and "temperature" in params:
            params.pop("temperature")
            return True
        return False


class LLMError(RuntimeError):
    """An LLM failure. `retryable` marks whether it's safe to fall over to another
    provider. Non-retryable errors (bad key, unsupported model, malformed request,
    safety refusal) surface immediately so real config bugs aren't masked."""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


# HTTP statuses worth retrying / failing over on (transient); everything else
# (401 auth, 403, 404 unsupported model, 400 malformed, 422) is a hard error.
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def _http_error(prefix: str, code: int, detail: str) -> "LLMError":
    return LLMError(f"{prefix} HTTP {code}: {detail}", retryable=code in _RETRYABLE_STATUS)
