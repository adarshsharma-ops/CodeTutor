#!/usr/bin/env python3
"""Connectivity smoke test — confirm your LLM endpoint works before anything else.

Run this first. It makes ONE tiny chat call and tells you if your key/URL/model are
wired correctly, so you can tell auth problems apart from mentor logic.

    export OPENAI_API_KEY=sk-...
    # OPENAI_BASE_URL defaults to https://api.openai.com/v1
    # MENTOR_MODEL   defaults to gpt-4o-mini
    python3 check_llm.py
"""
from __future__ import annotations

import sys

from mentor.config import Config
from mentor.llm import LLMClient, LLMError


def main() -> int:
    cfg = Config.from_env()
    print(f"providers   : {', '.join(cfg.available_providers()) or 'none (offline)'}")
    print(f"anthropic   : {'key set' if cfg.anthropic_key else '—'}  ({cfg.anthropic_base_url})")
    print(f"openai      : {'key set' if cfg.openai_key else '—'}  ({cfg.openai_base_url})")
    print(f"model       : {cfg.model}  (strong — teaching)")
    print(f"fast_model  : {cfg.fast_model}  (frequent hints)")

    if cfg.offline:
        print("\n[offline] No API key detected — the mentor will use mock responses.")
        print("Set ANTHROPIC_API_KEY (or OPENAI_API_KEY) in .env to test a real model.")
        return 0

    client = LLMClient(cfg)

    # 1) List the models available across all configured providers.
    print("\nFetching the models your key(s) can access...")
    try:
        models = client.list_models()
    except LLMError as e:
        print(f"\n❌ Could not list models: {e}")
        print("If this is a connection reset, you're likely behind a proxy/firewall "
              "that blocks the endpoint (e.g. a corporate network).")
        return 1

    if models:
        print(f"\n✅ {len(models)} models available:\n")
        # Highlight likely-good chat models first for convenience.
        chatty = [m for m in models if any(k in m for k in ("claude", "gpt", "o1", "o3", "o4"))]
        others = [m for m in models if m not in chatty]
        for m in chatty:
            print(f"   • {m}")
        if others:
            print("   ---")
            for m in others:
                print(f"   • {m}")

    # 2) Confirm the configured model actually works.
    print(f"\nTesting your configured model ({cfg.model})...")
    try:
        reply = client.chat(
            system="You are a terse assistant.",
            user="Reply with exactly: CodeTutor LLM OK",
            max_tokens=20,
        )
    except LLMError as e:
        print(f"❌ FAILED: {e}")
        print(f"'{cfg.model}' may not be a valid ID for your key — pick one from the list "
              f"above and set MENTOR_MODEL in .env.")
        return 1

    print(f"✅ SUCCESS. Model replied: {reply!r}")
    print("You're ready. Now run:  python3 eval_harness.py --out report_llm.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
