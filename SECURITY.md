# Security and privacy

## Supported-use boundary

CodeTutor is a local-development prototype, not a production or multi-user service. Run
the mentor service on loopback only. Do not expose it to a LAN or the public internet.

Do not process confidential, proprietary, regulated, personal, or third-party source code
unless you are authorized to send it to the configured model endpoint and have reviewed
that provider's retention, training, residency, and access terms.

## Data flow

The VS Code extension sends the current Python buffer to the local mentor service for
completed-line and idle events. With a real provider configured, the service sends code
context and learner-profile context to that provider. Hover-triggered requests are
disabled by default; the explicit "Why is this line here?" command remains available.

Learner observations are stored in `~/.codetutor/learner.db`. The database can reveal
learning activity and recurring mistakes. It should be treated as personal data. The
prototype supplies reset behavior but does not yet offer retention schedules, export, or
an in-editor deletion control.

## Secrets

Store provider credentials only in `mentor-service/.env` or the process environment.
`.env` is ignored by Git. Never put real values in `config.example.env`, screenshots,
logs, issues, or evaluation reports. Rotate a key immediately if it may have been exposed.

## Privacy & governance controls (implemented)

- Hover explanations are opt-in (`codetutor.enableHoverExplanations`).
- Never-send filename patterns (`codetutor.neverSend`) exclude secrets/keys/credentials.
- Function-only context scope (`codetutor.contextScope = function`) limits what is sent.
- A status-bar indicator shows activity and provides one-click pause (no sends while paused).
- Outbound editor actions use a shared privacy gate for pause and never-send enforcement.
- One-time consent before code is first sent off the machine.
- Automatic failover only triggers on transient errors, and records which provider served
  each response ("· via <model>") so a second-provider disclosure is visible, not silent.
- Learner competency is viewable and resettable ("Show my progress").

## Roadmap / deferred (not yet implemented)

- Per-workspace (not just per-install) consent and sensitivity-scoped failover.
- Local redaction of secrets detected *inside* a buffer (beyond filename patterns).
- Learner-facing correction of individual inferred competencies (not just full reset).
- Weighted, source-aware negative evidence beyond the current keyword mapping.
- Authentication, rate limiting, tenant isolation, and an external session store for any
  non-local deployment.

Terminology note: the UI says "observed consistently" rather than "mastered" — these are
heuristic signals of repeated clean use, not a validated assessment of understanding.

## Known limitations

- No service authentication or authorization
- No rate limiting or abuse controls
- In-memory sessions have no expiry or size bound
- No tenant isolation
- No formal prompt-injection defense; learner code is untrusted model input
- No guarantee that provider responses are correct or pedagogically safe
- No extension-level automated test suite yet

## Reporting a vulnerability

Open a GitHub security advisory after the public repository is created. Until a private
reporting channel exists, do not include secrets, proprietary code, or exploit data in a
public issue.
