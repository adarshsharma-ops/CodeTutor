# Set up a model provider

CodeTutor's extension talks to the local mentor service. The mentor service then uses
one of the options below. Never paste an API key into chat, source code, VS Code settings,
an issue, or a commit.

## Option 1: Local Ollama (free, no API key)

This is the easiest way to try CodeTutor without sharing a cloud key. Code and prompts
stay on the computer running Ollama, but this provider is **experimental for tutoring**.
Local models—especially smaller ones—can be noticeably less accurate, less aware of a
learner's intent, and less beginner-friendly than a strong hosted model. Use the quality
evaluation described below before recommending a local model to learners.

1. Install [Ollama](https://ollama.com/download).
2. Download a coding-capable model:

   ```bash
   ollama pull qwen2.5-coder:7b
   ```

3. In `mentor-service`, copy `config.example.env` to `.env`.
4. Set these values in `.env`:

   ```dotenv
   OPENAI_API_KEY=
   OPENAI_BASE_URL=http://127.0.0.1:11434/v1
   MENTOR_MODEL=qwen2.5-coder:7b
   MENTOR_MODEL_FAST=qwen2.5-coder:7b
   MENTOR_FAILOVER=0
   ```

5. Start or restart the mentor service.
6. Run `python3 check_llm.py` from `mentor-service` to verify the model.

The model is downloaded to the tester's computer and may require several gigabytes of
disk space. A smaller model is faster but may provide substantially weaker tutoring.
Ollama must be running while CodeTutor is in use. Run `python3 eval_harness.py --out
ollama-report.md` and review the report before treating a connectivity smoke test as a
teaching-quality pass; see [Tutor reliability](TUTOR_RELIABILITY.md).

## Option 2: OpenAI (bring your own key)

1. In `mentor-service`, copy `config.example.env` to `.env`.
2. Put your own key in `OPENAI_API_KEY`.
3. Keep `OPENAI_BASE_URL=https://api.openai.com/v1` and choose models available to your account.
4. Start or restart the mentor service, then run `python3 check_llm.py`.

Set a low provider-side spending limit before testing. Code context is sent to OpenAI.

## Option 3: Anthropic Claude (bring your own key)

1. In `mentor-service`, copy `config.example.env` to `.env`.
2. Put your own key in `ANTHROPIC_API_KEY`.
3. Keep `ANTHROPIC_BASE_URL=https://api.anthropic.com` and choose models available to your account.
4. Start or restart the mentor service, then run `python3 check_llm.py`.

Set a low provider-side spending limit before testing. Code context is sent to Anthropic.

## Where secrets live

The `.env` file is local and excluded by the repository's `.gitignore`. CodeTutor never
requires a key for Ollama. Cloud keys are read by the local Python service and are not
stored in the extension's settings.

If a key is ever committed or shared, revoke it at the provider immediately and create a
new one. Deleting it from the latest file is not enough because Git history may retain it.
