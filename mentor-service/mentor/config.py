"""Configuration — all LLM wiring is config-driven. Supports Anthropic (Claude),
OpenAI, any OpenAI-compatible gateway, or offline mock mode.

Set these via environment variables (see config.example.env):

    MENTOR_LLM_MODE   = "anthropic" | "openai" | "offline"   (default: auto-detect)

  Anthropic (Claude):
    ANTHROPIC_API_KEY = your key (sk-ant-...)
    ANTHROPIC_BASE_URL= default https://api.anthropic.com
    MENTOR_MODEL      = e.g. claude-sonnet-5
    MENTOR_MODEL_FAST = e.g. claude-haiku-4-5-20251001

  OpenAI / compatible:
    OPENAI_API_KEY    = your key / token
    OPENAI_BASE_URL   = default https://api.openai.com/v1
    MENTOR_MODEL      = e.g. gpt-5.4

  Shared:
    MENTOR_IDLE_SECONDS    = seconds idle before the "stuck" nudge (default 10)
    MENTOR_REQUEST_TIMEOUT = HTTP timeout in seconds (default 30)

Auto-detection: an Anthropic key wins; else an OpenAI key; else offline.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from mentor-service/.env into os.environ (no dependency).

    Existing environment variables always win, so `export` still overrides the file.
    """
    # mentor-service/ is the parent of the mentor/ package directory.
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


ANTHROPIC_VERSION = "2023-06-01"

# Sensible defaults so a bare key "just works".
_DEFAULT_MODEL = {"anthropic": "claude-sonnet-5", "openai": "gpt-4o-mini"}
_DEFAULT_FAST = {"anthropic": "claude-haiku-4-5-20251001", "openai": ""}


def provider_for(model: str) -> str:
    """Which provider serves a given model, inferred from its name."""
    return "anthropic" if model.strip().lower().startswith("claude") else "openai"


@dataclass
class Config:
    """Multi-provider config: OpenAI and Anthropic can both be active at once, and
    each request is routed to the right provider by model name."""
    openai_key: str
    openai_base_url: str
    anthropic_key: str
    anthropic_base_url: str
    model: str            # default strong model (high-value teaching)
    fast_model: str       # default fast model (frequent hints)
    idle_seconds: int
    request_timeout: int
    learner_db: str
    prompt_cache: bool = True   # cache the stable system prompt (Anthropic)
    failover: bool = True       # auto-retry on the other provider if the primary fails
    fallback_model: str = ""    # other-provider strong model to fall back to
    fallback_fast: str = ""     # other-provider fast model to fall back to

    @property
    def offline(self) -> bool:
        return not (self.openai_key or self.anthropic_key)

    def available_providers(self) -> list[str]:
        p = []
        if self.anthropic_key:
            p.append("anthropic")
        if self.openai_key:
            p.append("openai")
        return p

    def has_provider(self, provider: str) -> bool:
        return bool(self.anthropic_key if provider == "anthropic" else self.openai_key)

    def creds_for(self, provider: str) -> tuple[str, str]:
        """Return (api_key, base_url) for a provider."""
        if provider == "anthropic":
            return self.anthropic_key, self.anthropic_base_url
        return self.openai_key, self.openai_base_url

    @property
    def mode(self) -> str:
        """Human-readable summary for logs/health (not used for routing)."""
        if self.offline:
            return "offline"
        return "+".join(self.available_providers())

    def _chain(self, primary: str, fallback: str) -> list[str]:
        """Ordered list of models to try: primary first, then the other provider's
        equivalent (if failover is on and that provider has a key)."""
        out: list[str] = []
        candidates = [primary] + ([fallback] if self.failover else [])
        for m in candidates:
            if m and m not in out and self.has_provider(provider_for(m)):
                out.append(m)
        return out or [primary]

    def strong_chain(self) -> list[str]:
        return self._chain(self.model, self.fallback_model)

    def fast_chain(self) -> list[str]:
        return self._chain(self.fast_model, self.fallback_fast)

    @classmethod
    def from_env(cls) -> "Config":
        _load_dotenv()
        anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        openai_key = os.getenv("OPENAI_API_KEY", "").strip()

        # Default model: explicit, else based on whichever provider is present.
        default_provider = "anthropic" if anthropic_key else "openai"
        model = os.getenv("MENTOR_MODEL", "").strip() or _DEFAULT_MODEL[default_provider]
        fast_model = (os.getenv("MENTOR_MODEL_FAST", "").strip()
                      or _DEFAULT_FAST.get(default_provider, "") or model)

        # Failover: the OTHER provider (relative to the primary model) is the fallback.
        primary_provider = provider_for(model)
        other = "openai" if primary_provider == "anthropic" else "anthropic"
        other_has_key = bool(openai_key if other == "openai" else anthropic_key)
        fallback_model = os.getenv("MENTOR_FALLBACK_MODEL", "").strip()
        fallback_fast = os.getenv("MENTOR_FALLBACK_FAST", "").strip()
        if not fallback_model and other_has_key:
            fallback_model = _DEFAULT_MODEL[other]
        if not fallback_fast and other_has_key:
            fallback_fast = _DEFAULT_FAST.get(other, "") or _DEFAULT_MODEL[other]

        return cls(
            openai_key=openai_key,
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/"),
            anthropic_key=anthropic_key,
            anthropic_base_url=os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com").strip().rstrip("/"),
            model=model,
            fast_model=fast_model,
            idle_seconds=int(os.getenv("MENTOR_IDLE_SECONDS", "10")),
            request_timeout=int(os.getenv("MENTOR_REQUEST_TIMEOUT", "60")),
            learner_db=os.getenv("MENTOR_LEARNER_DB", "").strip(),
            prompt_cache=os.getenv("MENTOR_PROMPT_CACHE", "1").strip().lower()
            not in ("0", "false", "no", ""),
            failover=os.getenv("MENTOR_FAILOVER", "1").strip().lower()
            not in ("0", "false", "no", ""),
            fallback_model=fallback_model,
            fallback_fast=fallback_fast,
        )
