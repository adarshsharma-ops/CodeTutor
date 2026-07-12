"""Mentor — orchestrates the four behaviors on top of the analyzer + LLM + session.

Behaviors (each maps to a trigger the editor detects):
  1. blueprint(goal)           — goal stated       -> ordered plan
  2. on_completed_line(...)     — line finished     -> next-step hint w/ reasoning
  3. on_error(...)              — parser found typo -> explanation + fix (LLM phrases it)
  4. on_stuck(...)              — idle >= N seconds  -> gentle nudge (esp. unfinished line)

Every response is a MentorMessage the editor renders inline and/or in the side panel.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import List, Optional

from . import prompts
from . import curriculum
from .analyzer import analyze, Analysis, ContextIssue, CONCEPT_LABELS, concepts_from_line
from .llm import LLMClient
from .learner_model import LearnerModel
from .state import Session


@dataclass
class MentorMessage:
    kind: str                       # "blueprint" | "next_step" | "error" | "stuck" | ...
    text: str                       # the full explanation (shown in the chat panel)
    line: Optional[int] = None      # anchor line (for the above-line hint), 1-based
    curiosity: Optional[str] = None # optional "first time seeing X?" prompt
    blueprint: Optional[List[str]] = None
    headline: Optional[str] = None  # short, non-truncating hint for the line above the code
    via: Optional[str] = None       # which model/provider actually served this response

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


class Mentor:
    def __init__(self, llm: LLMClient, learner: LearnerModel | None = None):
        self.llm = llm
        self.learner = learner or LearnerModel()

    # Model selection returns an ORDERED CHAIN: primary first, then the other
    # configured provider as automatic failover. A per-session override (user picked a
    # model) wins and disables failover — an explicit choice is respected as-is.
    def _strong_chain(self, session: Session) -> list[str]:
        return [session.model_override] if session.model_override else self.llm.config.strong_chain()

    def _fast_chain(self, session: Session) -> list[str]:
        return [session.model_override] if session.model_override else self.llm.config.fast_chain()

    # --- 1. Blueprint -----------------------------------------------------
    def blueprint(self, session: Session) -> MentorMessage:
        via = None
        if self.llm.offline:
            steps = _offline_blueprint(session.goal)
        else:
            raw, via = self.llm.chat_with_failover(
                system=prompts.BLUEPRINT_SYSTEM,
                user=prompts.BLUEPRINT_USER.format(goal=session.goal),
                max_tokens=350,
                models=self._strong_chain(session),
            )
            steps = _parse_steps(raw)
        session.blueprint = steps
        return MentorMessage(kind="blueprint", text=session.blueprint_text(),
                             blueprint=steps, via=via)

    # --- 2. Completed line -> next step ----------------------------------
    def on_completed_line(self, session: Session, code: str,
                          target_line: int | None = None) -> Optional[MentorMessage]:
        a = analyze(code)
        # If there's a real syntax issue, that path takes priority.
        if a.has_syntax_issue:
            return self.on_error(session, code, a)
        # Valid Python can still encode a high-confidence placement mistake. Teach that
        # before suggesting more code, otherwise the learner builds on a broken idea.
        corrections = [i for i in a.context_issues if not i.ask_intent]
        questions = [i for i in a.context_issues if i.ask_intent]
        if corrections:
            return self.on_context_issue(session, code, corrections[0])
        if session.pending_correction:
            corrected = session.pending_correction
            session.pending_correction = ""
            session.awaiting_understanding = corrected
            session.hints_given += 1
            text = (
                "That placement issue is gone in this version. Python can now keep the "
                "value across repetitions or reach the statement in the order you intended. "
                "Before moving on, explain in your own words what changed and why it matters."
            )
            return MentorMessage(
                kind="understanding_check", text=text, line=_line_count(code),
                headline="Explain why this placement now works",
            )
        if questions:
            return self.on_context_issue(session, code, questions[0])

        # Update the persistent learner model: this buffer parsed cleanly. Pass the
        # structural fingerprints so only DISTINCT implementations advance mastery —
        # repeated/identical snapshots don't recount.
        self.learner.observe(session.learner_id, a.concepts, clean=True,
                             misconceptions=a.misconceptions, fingerprints=a.fingerprints)
        profile = self.learner.profile(session.learner_id)

        # Curiosity meter: only prompt for concepts the learner has NOT mastered.
        fresh = session.new_symbols(a.imports, a.called_names)
        curiosity = _curiosity_prompt(fresh, a, profile)

        via = None
        if self.llm.offline:
            text = _offline_next_step(a, session, profile)
        else:
            text, via = self.llm.chat_with_failover(
                system=prompts.NEXT_STEP_SYSTEM,
                user=prompts.NEXT_STEP_USER.format(
                    profile=profile.as_prompt_block(),
                    goal=session.goal,
                    blueprint=session.blueprint_text(),
                    code=code,
                    last_line=a.last_line,
                    curiosity=(f"Note: first use of {', '.join(fresh)}." if fresh else ""),
                ),
                models=self._fast_chain(session),  # high-frequency -> fast model, then failover
            )
        session.hints_given += 1
        return MentorMessage(
            kind="next_step",
            text=text,
            line=target_line or _line_count(code),
            curiosity=curiosity,
            headline=_headline_for(text, "next_step"),
            via=via,
        )

    def on_context_issue(self, session: Session, code: str,
                         issue: ContextIssue) -> MentorMessage:
        """Explain a valid-but-misplaced construct using what/where/why/how."""
        profile = self.learner.profile(session.learner_id)
        via = None
        if self.llm.offline:
            text = (
                f"{issue.summary}. {issue.explanation} Think about which instruction "
                "should happen before repetition starts, then move only that responsibility."
            )
        else:
            text, via = self.llm.chat_with_failover(
                system=prompts.CONTEXT_CORRECTION_SYSTEM,
                user=prompts.CONTEXT_CORRECTION_USER.format(
                    profile=profile.as_prompt_block(), goal=session.goal, code=code,
                    summary=issue.summary, line=issue.line,
                    related_line=issue.related_line, explanation=issue.explanation,
                ),
                models=self._strong_chain(session),
                max_tokens=500,
            )
        session.hints_given += 1
        if not issue.ask_intent:
            session.pending_correction = issue.code
        kind = "context_question" if issue.ask_intent else "context_correction"
        return MentorMessage(
            kind=kind, text=text, line=issue.line,
            headline=_headline_for(text, kind), via=via,
        )

    # --- 3. Error -> explanation -----------------------------------------
    def on_error(self, session: Session, code: str, analysis: Optional[Analysis] = None) -> MentorMessage:
        a = analysis or analyze(code)
        issue = a.syntax_issue
        if issue is None:
            # No real error; fall back to a next-step nudge.
            return self.on_completed_line(session, code) or MentorMessage(
                kind="next_step", text="Looks good so far — keep going.")

        # Record a normalized signature so repeated mistakes become a "recurring
        # misconception" — the moment we change strategy rather than repeat ourselves.
        signature = _error_signature(issue)
        self.learner.record_error_signature(session.learner_id, signature)
        recurring = self.learner.is_recurring(session.learner_id, signature)

        # Negative evidence: map the broken line to the concept being attempted (e.g.
        # `while r` -> loops) and record an error_hit, so repeated trouble with a concept
        # surfaces as "struggling". A recurring mistake weighs more than a one-off, which
        # might just be typing noise.
        self.learner.record_concept_errors(
            session.learner_id, concepts_from_line(issue.text or ""),
            weight=2 if recurring else 1)

        profile = self.learner.profile(session.learner_id)

        via = None
        if self.llm.offline:
            text = _offline_error(issue, recurring)
        else:
            recurring_note = (
                "IMPORTANT: the learner has made this same mistake several times. Do not just "
                "restate the fix — explain the underlying concept from a different angle so it "
                "finally clicks.\n" if recurring else "")
            text, via = self.llm.chat_with_failover(
                system=prompts.ERROR_SYSTEM,
                user=prompts.ERROR_USER.format(
                    profile=profile.as_prompt_block(),
                    code=code, message=issue.message, line=issue.line, text=issue.text,
                    recurring=recurring_note),
                models=self._strong_chain(session),
            )
        return MentorMessage(kind="error", text=text, line=issue.line,
                             headline=_headline_for(text, "error"), via=via)

    # --- 4. Stuck / idle -> nudge ----------------------------------------
    def on_stuck(self, session: Session, code: str, idle_seconds: int) -> MentorMessage:
        a = analyze(code)
        completeness = "complete" if a.last_line_complete else "unfinished"
        profile = self.learner.profile(session.learner_id)
        via = None
        if self.llm.offline:
            text = _offline_stuck(a, session)
        else:
            text, via = self.llm.chat_with_failover(
                system=prompts.STUCK_SYSTEM,
                user=prompts.STUCK_USER.format(
                    profile=profile.as_prompt_block(),
                    goal=session.goal,
                    blueprint=session.blueprint_text(),
                    code=code,
                    idle=idle_seconds,
                    last_line=a.last_line,
                    completeness=completeness,
                    condition_guidance=a.condition_hint or "none",
                ),
                # Stuck is infrequent but high-value — use the strong model.
                models=self._strong_chain(session),
            )
        session.hints_given += 1
        return MentorMessage(kind="stuck", text=text, line=_line_count(code),
                             headline=_headline_for(text, "stuck"), via=via)

    # --- Curriculum: suggest goals + report the learning path -------------
    def suggest_goals(self, learner_id: str, limit: int = 3) -> List[dict]:
        mastered = self.learner.mastered_concepts(learner_id)
        return [{"goal": s.goal, "rationale": s.rationale}
                for s in curriculum.suggest_goals(mastered, limit)]

    def learning_path(self, learner_id: str) -> dict:
        mastered = self.learner.mastered_concepts(learner_id)
        levels = [
            {"key": p.key, "title": p.title, "mastered": p.mastered,
             "total": p.total, "done": p.done, "blurb": p.blurb}
            for p in curriculum.path_progress(mastered)
        ]
        current = curriculum.current_level(mastered)
        return {"current_level": current.key, "levels": levels,
                "current_module": curriculum.module_details(current.key)}

    # --- 5. Curiosity payoff: explain a concept/symbol on demand ----------
    def explain(self, session: Session, target: str, code: str = "") -> MentorMessage:
        via = None
        if self.llm.offline:
            text = _offline_explain(target)
        else:
            profile = self.learner.profile(session.learner_id)
            text, via = self.llm.chat_with_failover(
                system=prompts.EXPLAIN_SYSTEM,
                user=prompts.EXPLAIN_USER.format(
                    profile=profile.as_prompt_block(), target=target,
                    goal=session.goal, code=code or "(none yet)"),
                max_tokens=300,
                models=self._strong_chain(session),
            )
        return MentorMessage(kind="explain", text=text, via=via)

    # --- 7. Free-form "ask the mentor" (chat input) -----------------------
    def ask(self, session: Session, question: str, code: str = "") -> MentorMessage:
        via = None
        if session.awaiting_understanding:
            pattern = session.awaiting_understanding
            session.awaiting_understanding = ""
            if self.llm.offline:
                text = ("Good—you're connecting placement with execution order. "
                        "Now try to say what Python would do on the second repetition.")
            else:
                text, via = self.llm.chat_with_failover(
                    system=prompts.UNDERSTANDING_SYSTEM,
                    user=prompts.UNDERSTANDING_USER.format(pattern=pattern, answer=question),
                    models=self._strong_chain(session), max_tokens=300,
                )
            return MentorMessage(kind="understanding_check", text=text, via=via)
        if self.llm.offline:
            text = (f"(offline) You asked: “{question}”. I'd answer this using your code and "
                    f"goal — connect a model to get a real reply.")
        else:
            profile = self.learner.profile(session.learner_id)
            text, via = self.llm.chat_with_failover(
                system=prompts.ASK_SYSTEM,
                user=prompts.ASK_USER.format(
                    profile=profile.as_prompt_block(), goal=session.goal,
                    code=code or "(none yet)", question=question),
                models=self._strong_chain(session),
                max_tokens=1024,  # room for a full answer on the strong model
            )
        return MentorMessage(kind="answer", text=text, via=via)

    # --- 6. "Why is this here?" — explain a specific line/symbol ----------
    def why(self, session: Session, code: str, line: int, symbol: Optional[str] = None) -> MentorMessage:
        text_line = _line_at(code, line)
        via = None
        if self.llm.offline:
            text = _offline_why(text_line, symbol)
        else:
            profile = self.learner.profile(session.learner_id)
            sym = f"\nThey specifically asked about the symbol `{symbol}`." if symbol else ""
            text, via = self.llm.chat_with_failover(
                system=prompts.WHY_SYSTEM,
                user=prompts.WHY_USER.format(
                    profile=profile.as_prompt_block(), code=code,
                    line=line, text=text_line, symbol=sym),
                models=self._strong_chain(session),
            )
        return MentorMessage(kind="why", text=text, line=line, via=via)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HEADLINE_MAX = 64
_GENERIC_HEADLINE = {
    "next_step": "Next step — details in chat",
    "stuck": "Stuck? — hint in chat",
    "error": "Syntax issue — details in chat",
    "why": "Why this line — details in chat",
    "answer": "Answer in chat",
    "explain": "Explanation in chat",
    "context_correction": "Check where this line belongs",
    "context_question": "Check what you want to repeat",
    "understanding_check": "Explain why your correction works",
}


def _headline_for(text: str, kind: str) -> str:
    """Produce a SHORT, complete headline for the line above the code — never truncated.

    Prefers the model's 'headline\\n\\nbody' format; else the first sentence if short
    enough; else a safe generic label. Guarantees length <= _HEADLINE_MAX with no '…'.
    """
    stripped = text.strip()
    # 1) Model followed the headline-first format.
    first_block = stripped.split("\n\n", 1)[0].strip()
    first_line = first_block.splitlines()[0].strip().lstrip("#*-• ").strip() if first_block else ""
    if first_line and len(first_line) <= _HEADLINE_MAX and "\n" not in first_line:
        return first_line.rstrip(".")
    # 2) First sentence, if it's short enough to show whole.
    sentence = re.split(r"(?<=[.!?])\s", stripped)[0] if stripped else ""
    if sentence and len(sentence) <= _HEADLINE_MAX:
        return sentence.rstrip(".")
    # 3) Guaranteed-short fallback.
    return _GENERIC_HEADLINE.get(kind, "Details in chat")


def _line_at(code: str, line: int) -> str:
    lines = code.splitlines()
    if 1 <= line <= len(lines):
        return lines[line - 1].strip()
    return ""


def _line_count(code: str) -> int:
    lines = code.splitlines()
    # anchor on the last non-empty line
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            return i + 1
    return len(lines) or 1


def _parse_steps(raw: str) -> List[str]:
    steps: List[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        # strip leading list markers like "1.", "-", "*"
        s = s.lstrip("0123456789.)-* \t").strip()
        # Models sometimes append conversational offers that are not plan steps.
        if s and not s.endswith("?") and not s.lower().startswith(("want to", "would you", "shall we")):
            steps.append(s)
    return (steps or [raw.strip()])[:8]


def _error_signature(issue) -> str:
    """Normalize a syntax error into a stable signature for recurrence counting."""
    text = (issue.text or "")
    if "improt" in text or "imort" in text or "imprt" in text:
        return "typo_import"
    msg = issue.message.lower()
    if "expected ':'" in msg or "expected an indented block" in msg:
        return "missing_colon"
    if "unterminated string" in msg or "eol while scanning" in msg:
        return "unterminated_string"
    if "'(' was never closed" in msg or "unexpected eof" in msg or "was never closed" in msg:
        return "unclosed_bracket"
    if "invalid syntax" in msg and ("=" in text and "==" not in text) and text.strip().startswith(("if", "while", "elif")):
        return "assignment_in_condition"
    return f"syntax:{msg[:40]}"


def _curiosity_prompt(fresh: List[str], analysis: Analysis, profile) -> Optional[str]:
    if not fresh:
        return None
    # Don't offer to explain concepts the learner has already mastered.
    relevant_concepts = analysis.concepts
    if relevant_concepts and all(profile.is_mastered(c) for c in relevant_concepts):
        return None
    first = fresh[0]
    return f"First time using {first} — want a 30-second explanation of why it exists? (Yes / No)"


# ---------------------------------------------------------------------------
# Offline / mock responses — deterministic, AST-informed. Let the whole loop run
# with zero setup. Not as smart as the LLM, but enough to feel the mechanics.
# ---------------------------------------------------------------------------

def _offline_blueprint(goal: str) -> List[str]:
    return [
        f"Clarify what '{goal}' needs as input and output — because the plan follows the data.",
        "Pick and import the libraries you'll need — so Python has the tools it lacks by default.",
        "Get the raw data (read a file, call an API, or take user input) — nothing works without input.",
        "Transform the data into a usable structure — so later steps can read values easily.",
        "Produce the result (print, save, or display) — this is the payoff of the goal.",
        "Handle the obvious failure (bad input, network error) — real programs meet the real world.",
    ]


def _offline_next_step(a: Analysis, session: Session, profile=None) -> str:
    line = a.last_line.strip()
    # Demonstrate learner-model filtering even in offline mode.
    if profile is not None:
        new_concepts = [CONCEPT_LABELS.get(c, c) for c in a.concepts
                        if not profile.is_mastered(c)]
        mastered_here = [CONCEPT_LABELS.get(c, c) for c in a.concepts
                         if profile.is_mastered(c)]
        if mastered_here and not new_concepts:
            return (f"You've got {', '.join(mastered_here)} down — I'll stay out of the way here. "
                    f"Move on to the next blueprint step.")
    if line.startswith("import ") or line.startswith("from "):
        mod = line.replace("from ", "").replace("import ", "").split()[0]
        return (f"You imported `{mod}`. Next you'll typically define the input it works on "
                f"(a URL, a filename, or a value) — because a library needs something to act on.")
    if "=" in line and "==" not in line:
        return ("You stored a value in a variable. Next, do something with it — pass it to a "
                "function or transform it — since a variable only helps once it's used.")
    if line.endswith(":"):
        return ("You opened a block (it ends with `:`). The next indented line is the body — "
                "what should happen for each item / when the condition holds?")
    return ("Nice — that line is complete. Think about the next blueprint step: what does the "
            "result of this line feed into?")


def _offline_stuck(a: Analysis, session: Session) -> str:
    if a.condition_hint:
        return a.condition_hint
    line = a.last_line.strip()
    if not a.last_line_complete:
        if line.endswith(" in") or line.endswith(" in "):
            return ("Stuck after `in`? A `for ... in` loop needs something *iterable* on the right — "
                    "a list, string, or range. Which collection from your plan holds the items to loop over?")
        if line.endswith("="):
            return ("You've started an assignment but haven't given it a value yet. What should the "
                    "right-hand side compute or hold?")
        if line.endswith("."):
            return ("You're reaching for a method with `.` — which action on this object do you want? "
                    "If unsure, that's normal: check the docs or autocomplete for what it offers.")
        return ("This line looks unfinished. What's the missing piece on the right-hand side, "
                "and what is it supposed to represent in your plan?")
    return ("Taking a pause? Glance at the blueprint — the next step is usually to use what you "
            "just created. What does this value feed into?")


_OFFLINE_CONCEPT_NOTES = {
    "requests": ("`requests` is a library for making HTTP calls. Python's standard library "
                 "can do it but is clunky; `requests` gives you simple `get()`/`post()` "
                 "functions. Example: `requests.get('https://...')` fetches a page."),
    "json": ("JSON is a text format for structured data that servers speak. `.json()` (or "
             "the `json` module) turns that text into Python dicts/lists so you can read "
             "values like `data['temperature']`."),
    "get()": ("`get()` sends an HTTP GET request — the 'read/fetch' verb of the web. It "
              "lives in the `requests` library. You'd find it via the docs or autocomplete."),
    "json()": ("`.json()` on a response parses the server's JSON text into a Python dict. "
               "It exists so you don't hand-parse text. Example: `data = response.json()`."),
}


def _offline_explain(target: str) -> str:
    key = target.strip().strip("`")
    note = _OFFLINE_CONCEPT_NOTES.get(key) or _OFFLINE_CONCEPT_NOTES.get(key.rstrip("()"))
    if note:
        return note
    return (f"`{key}` is something you just used for the first time. In short: it's a tool "
            f"that solves a specific problem so you don't have to build it yourself. "
            f"(Connect a real LLM for a richer, context-aware explanation.)")


def _offline_why(line: str, symbol: Optional[str]) -> str:
    focus = f"`{symbol}` in " if symbol else ""
    return (f"This line {focus}`{line}` does a specific job at this point in the program. "
            f"It's necessary because later lines depend on what it produces; remove it and "
            f"the code that uses its result would fail with a NameError or similar. "
            f"(Connect a real LLM for a precise, code-specific answer.)")


def _offline_error(issue, recurring: bool = False) -> str:
    msg = issue.message.lower()
    text = issue.text or ""
    if recurring:
        return (f"You've hit this a few times now, so let's slow down and look at it differently. "
                f"On line {issue.line}, `{text.strip()}` — instead of just fixing it, notice the "
                f"*pattern*: Python reads code strictly left-to-right against fixed grammar rules. "
                f"When something doesn't match a rule it knows, it stops right there. Try reading "
                f"this line the way Python does, token by token, and find the first token that "
                f"doesn't belong.")
    if "improt" in text or "imort" in text or "imprt" in text:
        return (f"Looks like a typo of `import` on line {issue.line}. Python only recognizes the exact "
                f"keyword `import`; anything else is treated as a name it doesn't know, so it throws a "
                f"SyntaxError. Fix it to `import ...`.")
    if "invalid syntax" in msg:
        return (f"Line {issue.line} won't parse: `{issue.text.strip()}`. Python hit something it "
                f"didn't expect here — check for a typo, a missing `:`, or an unclosed bracket, "
                f"because the parser reads left-to-right and stops at the first thing that breaks the rules.")
    return (f"Syntax error on line {issue.line}: {issue.message}. The line `{issue.text.strip()}` breaks "
            f"a Python rule — most often a missing colon, quote, comma, or bracket. Compare it "
            f"carefully against a working example of the same statement.")
