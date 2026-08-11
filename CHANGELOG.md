# Changelog

All notable changes will be recorded here. The project follows semantic versioning once
public releases begin.

## [0.2.0] - 2026-08-11

### Added

- Added separate Beginner, Intermediate, and Advanced teaching modes that can be changed
  during a session.
- Added an opt-in AI Engineer & AI Expert journey spanning Python foundations, data and
  model thinking, dependable LLM applications, retrieval, agents, evaluations and
  governance, and production operations.
- Added versioned Python Foundations, Python for Data, and Machine Learning Foundations
  pathway catalogs with prerequisites, evidence, common mistakes, and understanding checks.
- Added first-run provider setup for Anthropic Claude, OpenAI, and keyless local Ollama,
  including local provider detection and safe credential storage in the ignored `.env` file.
- Added durable lesson state, Continue lesson, deterministic Run lesson check, Next lesson,
  learning review, and saved curriculum progress.
- Added explicit Ask, Explain, and consent-gated Fix this line actions in the editor.
- Added contextual correction for high-confidence Python placement mistakes, incomplete
  conditions, missing operators, unfinished imports, and likely implicit `None` returns.
- Added a model evaluation harness and reliability documentation for comparing tutoring
  quality rather than treating connectivity as a quality result.

### Improved

- Made Beginner blueprints concrete and goal-aware, with the next action, its purpose, and
  progressive help when a learner remains stuck.
- Anchored inline hints to the relevant source line and kept the blueprint visible while
  the independently scrollable conversation grows.
- Prevented overlapping or stale automatic interventions from rapidly replacing useful
  guidance, while keeping explicit learner questions immediate.
- Grounded completion in deterministic syntax, project, return-path, and successful-run
  evidence; model prose can no longer mark an unfinished program complete.
- Added provider attribution, context minimization, pause and never-send controls, and
  beginner-readable error handling.
- Expanded deterministic analyzer and mentor coverage to 150 offline tests.
