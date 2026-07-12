"""Tests for multi-provider config, routing, and per-session model override."""
from mentor.config import Config, provider_for
from mentor.llm import LLMClient
from mentor.learner_model import LearnerModel
from mentor.mentor import Mentor
from mentor.state import Session


def _cfg(anthropic="", openai="", model="claude-sonnet-5", fast="claude-haiku-4-5-20251001"):
    return Config(
        openai_key=openai, openai_base_url="https://api.openai.com/v1",
        anthropic_key=anthropic, anthropic_base_url="https://api.anthropic.com",
        model=model, fast_model=fast, idle_seconds=10, request_timeout=30, learner_db="",
    )


def test_provider_inferred_from_model_name():
    assert provider_for("claude-sonnet-5") == "anthropic"
    assert provider_for("claude-opus-4-8") == "anthropic"
    assert provider_for("gpt-5.4") == "openai"
    assert provider_for("gpt-4o-mini") == "openai"
    assert provider_for("o3") == "openai"


def test_offline_when_no_keys():
    assert _cfg().offline is True
    assert _cfg(anthropic="sk-ant").offline is False


def test_available_providers_lists_both():
    c = _cfg(anthropic="sk-ant", openai="sk-oai")
    assert set(c.available_providers()) == {"anthropic", "openai"}


def test_creds_routing():
    c = _cfg(anthropic="A", openai="O")
    assert c.creds_for("anthropic")[0] == "A"
    assert c.creds_for("openai")[0] == "O"


def test_missing_provider_key_raises_helpful_error(tmp_path):
    # Anthropic-only config, but ask for an OpenAI model.
    c = _cfg(anthropic="sk-ant", model="claude-sonnet-5")
    client = LLMClient(c)
    try:
        client.chat("sys", "hi", model="gpt-5.4")
        assert False, "expected an error"
    except Exception as e:
        assert "openai" in str(e).lower()


def test_empty_response_raises():
    from mentor.llm import _require_nonempty, LLMError
    assert _require_nonempty("hello") == "hello"
    for blank in ("", "   ", "\n\t"):
        try:
            _require_nonempty(blank)
            assert False, "expected LLMError on empty response"
        except LLMError:
            pass


def test_failover_chain_lists_both_providers():
    c = _cfg(anthropic="A", openai="O", model="claude-sonnet-5", fast="claude-haiku-4-5-20251001")
    c.failover = True
    c.fallback_model = "gpt-5.4"
    c.fallback_fast = "gpt-5.4-mini"
    assert c.strong_chain() == ["claude-sonnet-5", "gpt-5.4"]
    assert c.fast_chain() == ["claude-haiku-4-5-20251001", "gpt-5.4-mini"]


def test_error_retryable_classification():
    from mentor.llm import LLMError, _http_error
    assert LLMError("x").retryable is True                       # default: transient
    assert LLMError("bad key", retryable=False).retryable is False
    assert _http_error("LLM", 429, "rate").retryable is True     # rate limit -> retry
    assert _http_error("LLM", 503, "down").retryable is True     # 5xx -> retry
    assert _http_error("LLM", 401, "auth").retryable is False    # auth -> hard fail
    assert _http_error("LLM", 404, "no model").retryable is False


def test_failover_off_uses_primary_only():
    c = _cfg(anthropic="A", openai="O", model="claude-sonnet-5")
    c.failover = False
    c.fallback_model = "gpt-5.4"
    assert c.strong_chain() == ["claude-sonnet-5"]


def test_chain_skips_fallback_without_key():
    # No OpenAI key -> the GPT fallback is dropped from the chain.
    c = _cfg(anthropic="A", openai="", model="claude-sonnet-5")
    c.failover = True
    c.fallback_model = "gpt-5.4"
    assert c.strong_chain() == ["claude-sonnet-5"]


def test_prompt_cache_flag_defaults_on():
    assert _cfg(anthropic="sk-ant").prompt_cache is True


def test_stuck_uses_strong_model(tmp_path):
    c = _cfg(anthropic="sk-ant", model="claude-sonnet-5", fast="claude-haiku-4-5-20251001")
    m = Mentor(LLMClient(c), LearnerModel(str(tmp_path / "m.db")))
    s = Session(goal="x")
    # The strong chain begins with the strong model.
    assert m._strong_chain(s)[0] == "claude-sonnet-5"


def test_session_model_override_wins(tmp_path):
    c = _cfg(anthropic="sk-ant", openai="sk-oai", model="claude-sonnet-5",
             fast="claude-haiku-4-5-20251001")
    m = Mentor(LLMClient(c), LearnerModel(str(tmp_path / "m.db")))
    s = Session(goal="x")
    # No override: chains start from config.
    assert m._strong_chain(s)[0] == "claude-sonnet-5"
    assert m._fast_chain(s)[0] == "claude-haiku-4-5-20251001"
    # Override applies to BOTH tiers and disables failover (single-model chain).
    s.model_override = "gpt-5.4"
    assert m._strong_chain(s) == ["gpt-5.4"]
    assert m._fast_chain(s) == ["gpt-5.4"]
