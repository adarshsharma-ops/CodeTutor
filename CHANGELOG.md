# Changelog

All notable changes will be recorded here. The project follows semantic versioning once
public releases begin.

## Unreleased

- Fixed restart discovery when VS Code restores focus outside the Python editor, and
  re-armed idle guidance after all unfinished lesson resumes, even if the initial code
  check intentionally returns no hint.
- Added Continue saved lesson and Open tutor panel commands, visible resume errors,
  and preserved completed-lesson actions without restarting coaching.
- Bound lesson chat, hints, fixes, and checks to the selected Python document, displayed
  its filename, and added an explicit command to attach a saved or replacement file.
- Discarded chat and hint replies when their lesson or document version changes.
- Added explicit pasted-code review without awarding lesson progress from pasted snippets.
- Replaced converter magic-number checks with parsed arithmetic checks supporting renamed
  variables and equivalent formulas; scripts no longer require an unannounced function.
- Added conservative branch-assignment and explicit conversion-menu checks, including
  invalid input that reaches an unassigned result.
- Kept readiness in a persistent status area instead of replacing current guidance.
- Added a model-independent Adaptive Intervention Engine that classifies observable
  learner state as progressing, thinking, stuck, off-path, or error before an LLM is
  allowed to phrase guidance.
- Added Current Guidance controls for another hint, a similar example, a one-minute
  quiet period, and consent-gated line repair when Python reports a verified error.
- Added intervention outcome tracking: a subsequent code change records progress, while
  a request for deeper help records that the previous hint was insufficient.
- Added deterministic learner-trace replay tests for state accuracy, interruption timing,
  confidence, and progressive-help availability.
- Added a beginner-first installation guide with complete macOS/Linux and Windows
  commands, expected output, model onboarding, relaunch steps, and common fixes.
- Made Current Guidance the dominant panel area and moved earlier guidance into a
  collapsed, independently scrollable history drawer with a constrained resize handle.
- Removed the hidden blank-file requirement: new journeys can create their own Python
  lesson file, attach to the current file, or reopen a saved lesson file.
- Added a webview readiness handshake so initial pathway, blueprint, and guidance events
  cannot be lost while the lesson editor is opening.
- Added supported Python-run observation and a prominent one-click deterministic
  verification flow; completed lessons now expose Next lesson inside Current Guidance.

## [0.2.0] - 2026-08-11

### Added

- Added separate Beginner, Intermediate, and Advanced teaching modes that can be changed
  during a session.
- Added an opt-in Structured AI Engineering Learning Path spanning Python foundations, data and
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
