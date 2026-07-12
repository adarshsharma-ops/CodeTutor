# Architecture & design notes

## The core idea

A mentor that lets you drive, intervenes only when it adds value, explains in context,
and gets quieter as your confidence grows. It is a **genie** (lives inside a host app —
your editor) not a standalone app. The editor is a solved problem; the mentor is the
product, so we build a VS Code **extension** rather than a new IDE.

## Why the work splits the way it does

**Two layers, deliberately separated:**

1. **Deterministic layer (cheap, fast, reliable) — no LLM.**
   Python's own parser (`compile` / `ast`) finds typos and syntax errors better than any
   model and for free. It also decides whether the last line is *complete* or *unfinished*
   (`for i in ` is unfinished, not an error) — which is what lets the stuck-nudge know how
   to help. It extracts imports/calls to drive the curiosity meter.

2. **LLM layer (open-ended teaching) — reserved for reasoning.**
   Blueprint generation, next-step hints with reasoning, stuck nudges, and *phrasing*
   error explanations the parser already found. Routing every keystroke through an LLM
   would be slow, expensive, and prone to hallucinating syntax rules. This split avoids
   all three.

**The blueprint is state, not a message.** It's a checklist the mentor tracks so
"next step" is always relative to where you actually are. Session state also remembers
which symbols you've seen (curiosity meter) and how many hints you've had (fade-over-time).

## The learner model — the central product hypothesis

The model provider is replaceable; the differentiating hypothesis is a **persistent,
per-learner model** of observed practice. It lives in `learner_model.py` (local SQLite)
and filters mentor prompts. The current signals are proxies and do not prove understanding.

**Concept lifecycle:** `unseen → exposed → practiced → mastered`, with a side state of
`struggling`. Evidence rules are deliberately simple (tune later):

- `analyzer.detect_concepts` maps the AST to concepts (loops, dicts, recursion, async,
  functions, classes, exceptions, HTTP, JSON, file I/O). Deterministic — no LLM.
- A concept becomes **mastered** after `MASTERY_CLEAN_USES` (3) appearances in code that
  parsed cleanly. Once mastered, the mentor stops re-explaining it and the curiosity
  meter skips it.

**Misconception recurrence:** every syntax error is normalized to a stable *signature*
(`typo_import`, `missing_colon`, `unclosed_bracket`, ...). Counting is pure tutoring-layer
logic — the LLM never sees it. When a signature hits `MISCONCEPTION_THRESHOLD` (3), the
mentor switches from "here's the fix" to a different, conceptual explanation strategy.
Some misconceptions (`mutable_default_arg`, `bare_except`) are caught structurally from
the AST even when the code parses.

**Prompt filtering:** `LearnerProfile.as_prompt_block()` produces a compact block
("Mastered: loops, lists. Struggling: recursion. Recurring: misspelling `import`.")
injected into the next-step, stuck, and error prompts.

### The two hard parts (called out honestly)

1. **Concept detection reliability.** "Used a dict" is easy; "*understands* recursion" is
   fuzzy. The current rule (N clean uses) is a proxy, not proof. This is where to invest.
2. **Cold-start & staleness.** A new learner has an empty model (mentor over-explains at
   first — acceptable). Mastery currently never decays; `last_seen` is stored so a decay
   rule can be added later.

## The four triggers (and where each is detected)

| Behavior | Detected by | Handled by |
|---|---|---|
| Blueprint | `codetutor.start` command (you set a goal) | LLM |
| Next-step hint | Extension debounce timer (you stopped typing) | LLM, guided by blueprint + code |
| Error coach | Python parser in the service | Parser finds it; LLM phrases it |
| Stuck nudge | Extension idle timer (~10s, resets on keystroke) | LLM, with completeness signal |

### Contextual correction

Syntax validity is not the same as correct placement. The deterministic analyzer therefore
also reports a small set of high-confidence structural findings in valid Python. The first
rules detect numbers or collections recreated inside their loop, an unconditional return
that stops a loop after one item, and statements that cannot execute after `return`,
`raise`, `break`, or `continue`. The LLM receives the proven Python behavior
and turns it into a beginner-level what/where/why/how explanation; it does not invent the
finding. Ambiguous intent should produce a checking question rather than a confident claim.

After the structural finding disappears, the session emits an understanding check asking
the learner to explain the correction in their own words. This is deliberately separate
from evidence of mastery: removing a warning once does not prove durable understanding.
The learner's next chat response is evaluated as an explanation of that corrected pattern,
so the tutor can reinforce the accurate part or ask one focused follow-up question.

Not every placement is objectively wrong. Intent-dependent patterns are represented as
questions: a `print` after a loop, for example, runs once with the final value, while the
same line inside runs once per item. The analyzer proves that behavioral difference and
the tutor asks which result was intended.

**Timing is the real UX risk.** A hint per keystroke is unbearable. The extension
debounces (waits for a pause) before a "completed" reaction, and only fires the stuck
nudge after a full idle window with no typing. It also suppresses re-firing the *same*
stuck nudge, so reading a hint and thinking doesn't get you nagged again.

## Deployment and trust boundaries

Three pieces, three homes:

- **Extension** — runs on each laptop. Distributed as a `.vsix` (public marketplace or an
  internal extension registry). Not "hosted."
- **Python mentor service** — currently started separately and accessed on localhost.
  Localhost keeps the service boundary narrow, although code can still leave the machine
  when a remote model provider is configured.
- **LLM** — config-driven via `OPENAI_BASE_URL`.

The trust path is `editor → local mentor service → configured model endpoint`. Source code
is untrusted and potentially confidential input. Users must understand the endpoint's
retention, training, residency, and access policies before enabling a real model.

A shared deployment is a different product boundary, not a configuration toggle. It would
require authentication, authorization, tenant isolation, encryption, retention and
deletion controls, rate limiting, auditability, abuse controls, and an external expiring
session store. None of those claims are made by this prototype.

## What's intentionally simple in this prototype (and how to harden it)

- **Session store is in-memory and unbounded** → add expiry and bounded storage before
  long-running or shared use.
- **Offline mode uses templated responses** → good for wiring/tests; real teaching quality
  comes from the LLM path.
- **One "completed line" heuristic** → could use tree-sitter for multi-language support
  and richer structural awareness beyond Python.
- **Curiosity meter is a simple first-seen set** → could weight by concept difficulty.
- **Fade model is a hint counter** → could incorporate error rate / time-to-complete.
- **No authentication on the service** → bind to localhost; add authentication and
  authorization before any shared deployment.
- **Automatic events include the current buffer** → disclose this clearly and provide
  granular controls. Hover-based requests are opt-in; completed-line and idle controls
  need further UX work.

## Suggested next steps

1. Compare hint quality across the offline baseline and an endpoint you are authorized to
   use, with synthetic code only.
2. Harden concept detection (evidence rules) and add a mastery-decay rule using `last_seen`.
3. Add multi-language support (tree-sitter) if learners go beyond Python.
4. Add a "why is this here?" richer hover that diffs against the previous buffer state.
5. Add data controls and authentication before designing any shared deployment.
