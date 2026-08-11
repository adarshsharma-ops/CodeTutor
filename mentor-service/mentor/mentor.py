"""Mentor — orchestrates the four behaviors on top of the analyzer + LLM + session.

Behaviors (each maps to a trigger the editor detects):
  1. blueprint(goal)           — goal stated       -> ordered plan
  2. on_completed_line(...)     — line finished     -> next-step hint w/ reasoning
  3. on_error(...)              — parser found typo -> explanation + fix (LLM phrases it)
  4. on_stuck(...)              — idle >= N seconds  -> gentle nudge (esp. unfinished line)

Every response is a MentorMessage the editor renders inline and/or in the side panel.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, asdict
from typing import List, Optional

from . import prompts
from . import curriculum
from .analyzer import analyze, Analysis, ContextIssue, CONCEPT_LABELS, concepts_from_line
from .llm import LLMClient, LLMError
from .learner_model import LearnerModel
from .lesson_progress import possible_implicit_none_functions
from .state import Session
from .reliability import (advice_key, beginnerize, facts_for_prompt, hint_policy,
                          response_is_grounded, safe_automatic_fallback)


@dataclass
class MentorMessage:
    kind: str                       # "blueprint" | "next_step" | "error" | "stuck" | ...
    text: str                       # the full explanation (shown in the chat panel)
    line: Optional[int] = None      # anchor line (for the above-line hint), 1-based
    curiosity: Optional[str] = None # optional "first time seeing X?" prompt
    blueprint: Optional[List[str]] = None
    headline: Optional[str] = None  # short, non-truncating hint for the line above the code
    via: Optional[str] = None       # which model/provider actually served this response
    replacement: Optional[str] = None  # explicit, learner-requested line replacement only
    escalation_level: Optional[int] = None

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
            steps = _offline_blueprint(session.goal, session.learner_level)
        else:
            try:
                raw, via = self.llm.chat_with_failover(
                    system=prompts.BLUEPRINT_SYSTEM,
                    user=prompts.BLUEPRINT_USER.format(goal=session.goal, level=session.learner_level),
                    max_tokens=600,
                    models=self._strong_chain(session),
                )
                steps = _parse_steps(raw)
                problems = _blueprint_quality_problems(steps, session.learner_level)
                if problems:
                    repaired, repair_via = self.llm.chat_with_failover(
                        system=prompts.BLUEPRINT_REPAIR_SYSTEM,
                        user=prompts.BLUEPRINT_REPAIR_USER.format(
                            goal=session.goal, level=session.learner_level,
                            problems="; ".join(problems), draft="\n".join(steps)),
                        max_tokens=650, models=self._strong_chain(session),
                    )
                    candidate = _parse_steps(repaired)
                    if not _blueprint_quality_problems(candidate, session.learner_level):
                        steps = candidate
                        via = f"{repair_via} · blueprint rewrite"
                    else:
                        steps = _offline_blueprint(session.goal, session.learner_level)
                        via = "reviewed fallback after blueprint validation"
            except LLMError:
                steps = _offline_blueprint(session.goal, session.learner_level)
                via = "reviewed fallback because the selected model was unavailable"
        session.blueprint = steps
        return MentorMessage(kind="blueprint", text=session.blueprint_text(),
                             blueprint=steps, via=via)

    # --- 2. Completed line -> next step ----------------------------------
    def on_completed_line(self, session: Session, code: str,
                          target_line: int | None = None) -> Optional[MentorMessage]:
        session.observe_progress(code)
        a = analyze(code)
        _update_current_step(session, a)
        if a.last_line.strip().endswith(":"):
            return None
        # If there's a real syntax issue, that path takes priority.
        if a.has_syntax_issue:
            # A just-opened block is normal composition, not a mistake. The idle path
            # can help later if the learner genuinely remains there.
            issue_text = (a.syntax_issue.message if a.syntax_issue else "").lower()
            if "expected an indented block" in issue_text and a.last_line.strip().endswith(":"):
                return None
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

        key = advice_key("next_step", a)
        level = session.next_hint_level(key)
        via = None
        if self.llm.offline or self.llm.config.local_openai:
            text = _offline_next_step(a, session, profile)
        else:
            text, via = self.llm.chat_with_failover(
                system=prompts.NEXT_STEP_SYSTEM,
                user=prompts.NEXT_STEP_USER.format(
                    profile=profile.as_prompt_block(),
                    goal=session.goal,
                    level=session.learner_level,
                    current_step=session.current_step_text(),
                    blueprint=session.blueprint_text(),
                    code=code,
                    facts=facts_for_prompt(a),
                    last_line=a.last_line,
                    hint_level=level,
                    hint_policy=hint_policy(level),
                    curiosity=(f"Note: first use of {', '.join(fresh)}." if fresh else ""),
                ),
                models=self._fast_chain(session),  # high-frequency -> fast model, then failover
            )
        if not response_is_grounded(text, a, automatic=True, level=level):
            text = safe_automatic_fallback("next_step", a, level)
            via = "verified fallback"
        text = beginnerize(text)
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
        analysis = analyze(code)
        key = advice_key("context", analysis, issue)
        level = session.next_hint_level(key)
        via = None
        if session.learner_level == "beginner" and not issue.ask_intent:
            text = _beginner_issue_message(issue)
            via = "verified beginner guidance"
        elif self.llm.offline or self.llm.config.local_openai:
            text = safe_automatic_fallback("context", analysis, level, issue)
            via = "verified local guidance" if self.llm.config.local_openai else None
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
        if not response_is_grounded(text, analysis, automatic=True, level=level):
            text = safe_automatic_fallback("context", analysis, level, issue)
            via = "verified fallback"
        text = beginnerize(text)
        session.hints_given += 1
        if not issue.ask_intent:
            session.pending_correction = issue.code
        kind = "context_question" if issue.ask_intent else "context_correction"
        semantic_headlines = {
            "method_reference_not_called": "Call the text method with parentheses",
            "discarded_return_value": "Use the value returned by this function",
            "input_outside_loop": "Read a fresh choice inside the loop",
            "prompt_string_used_as_value": "Use input() to collect the learner's answer",
        }
        return MentorMessage(
            kind=kind, text=text, line=issue.line,
            headline=semantic_headlines.get(issue.code, _headline_for(text, kind)), via=via,
        )

    # --- 3. Error -> explanation -----------------------------------------
    def on_error(self, session: Session, code: str, analysis: Optional[Analysis] = None) -> MentorMessage:
        a = analysis or analyze(code)
        issue = a.syntax_issue
        if issue is None:
            # No real error; fall back to a next-step nudge.
            return self.on_completed_line(session, code) or MentorMessage(
                kind="next_step", text="Looks good so far — keep going.")

        # Record only stable mistakes as recurrence evidence. Missing bodies, unfinished
        # strings and open brackets commonly occur while a learner is still typing.
        signature = _error_signature(issue)
        transient = any(part in issue.message.lower() for part in (
            "expected an indented block", "unterminated string", "was never closed",
            "unexpected eof",
        ))
        error_observation = f"{signature}:{code.strip()}"
        new_error_observation = error_observation != session.last_recorded_error_code
        if not transient and new_error_observation:
            self.learner.record_error_signature(session.learner_id, signature)
            session.last_recorded_error_code = error_observation
        recurring = False if transient else self.learner.is_recurring(session.learner_id, signature)

        # Negative evidence: map the broken line to the concept being attempted (e.g.
        # `while r` -> loops) and record an error_hit, so repeated trouble with a concept
        # surfaces as "struggling". A recurring mistake weighs more than a one-off, which
        # might just be typing noise.
        if not transient and new_error_observation:
            self.learner.record_concept_errors(
                session.learner_id, concepts_from_line(issue.text or ""),
                weight=2 if recurring else 1)

        profile = self.learner.profile(session.learner_id)

        via = None
        if self.llm.offline or self.llm.config.local_openai:
            text = _offline_error(issue, recurring)
            if self.llm.config.local_openai:
                via = "Python parser"
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
        session.observe_progress(code)
        a = analyze(code)
        _update_current_step(session, a)
        completeness = "complete" if a.last_line_complete else "unfinished"
        profile = self.learner.profile(session.learner_id)
        key = advice_key("stuck", a)
        level = session.next_hint_level(key)
        via = None
        guided = _beginner_composition_hint(session, a, level) or _guided_subgoal_hint(session, a, level)
        if guided:
            text = guided
            via = "guided subgoal"
        elif a.last_line.strip() in {"import", "from"}:
            # Choosing a library is a goal/intent question, not a syntax diagnosis.
            # Keep this deterministic so a small model cannot invent a package.
            text = _import_pause_guidance(session.goal, level, session.learner_level)
            via = "goal-aware guidance"
        elif a.has_syntax_issue and not (
            "expected an indented block" in (a.syntax_issue.message if a.syntax_issue else "").lower()
            and a.last_line.strip().endswith(":")
        ):
            # Syntax has priority over generic idle coaching. The active coaching card
            # will be replaced with this one verified diagnosis, not an additional bubble.
            return self.on_error(session, code, a)
        elif self.llm.offline or self.llm.config.local_openai:
            text = _offline_stuck(a, session, level)
            via = "verified local guidance"
        else:
            text, via = self.llm.chat_with_failover(
                system=prompts.STUCK_SYSTEM,
                user=prompts.STUCK_USER.format(
                    profile=profile.as_prompt_block(),
                    goal=session.goal,
                    level=session.learner_level,
                    current_step=session.current_step_text(),
                    blueprint=session.blueprint_text(),
                    code=code,
                    facts=facts_for_prompt(a),
                    idle=idle_seconds,
                    last_line=a.last_line,
                    completeness=completeness,
                    condition_guidance=a.condition_hint or "none",
                    hint_level=level,
                    hint_policy=hint_policy(level),
                ),
                # Stuck is infrequent but high-value — use the strong model.
                models=self._strong_chain(session),
            )
        validation_level = 4 if guided else level
        if not response_is_grounded(text, a, automatic=True, level=validation_level):
            text = safe_automatic_fallback("stuck", a, level)
            via = "verified fallback"
        text = beginnerize(text)
        session.hints_given += 1
        return MentorMessage(kind="stuck", text=text, line=_line_count(code),
                             headline=_headline_for(text, "stuck"), via=via,
                             escalation_level=level)

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
        fallback = _implicit_none_answer(session, question, code)
        if fallback:
            return MentorMessage(kind="answer", text=fallback, via="verified project check")
        diagnostic = _deterministic_diagnostic_answer(session, question, code)
        if diagnostic:
            return MentorMessage(kind="answer", text=diagnostic, via="Python-aware diagnosis")
        guided = _explicit_guided_subgoal_answer(session, question, code)
        if not guided:
            guided = _goal_aware_beginner_answer(session, question, code)
        if guided:
            text = guided
            via = "goal-aware guidance"
        elif self.llm.offline:
            text = (f"(offline) You asked: “{question}”. I'd answer this using your code and "
                    f"goal — connect a model to get a real reply.")
        else:
            profile = self.learner.profile(session.learner_id)
            analysis = analyze(code or "")
            text, via = self.llm.chat_with_failover(
                system=prompts.ASK_SYSTEM,
                user=prompts.ASK_USER.format(
                    profile=profile.as_prompt_block(), goal=session.goal,
                    level=session.learner_level,
                    current_step=session.current_step_text(),
                    code=code or "(none yet)", facts=facts_for_prompt(analysis),
                    question=question),
                models=self._strong_chain(session),
                max_tokens=1024,  # room for a full answer on the strong model
            )
            if not response_is_grounded(text, analysis, automatic=False):
                text = ("I can't verify that explanation from your current code, so I won't "
                        "guess. Point me to the line you are unsure about, and I'll use Python's "
                        "structure to explain one step at a time.")
                via = "safety check"
            text = beginnerize(text)
        return MentorMessage(kind="answer", text=text, via=via)

    # --- 6. "Why is this here?" — explain a specific line/symbol ----------
    def why(self, session: Session, code: str, line: int, symbol: Optional[str] = None) -> MentorMessage:
        text_line = _line_at(code, line)
        via = None
        verified = _deterministic_why(code, line, symbol)
        if verified:
            text = verified
            via = "Python-aware explanation"
        elif self.llm.offline or self.llm.config.local_openai:
            text = _offline_why(text_line, symbol)
            via = "verified local explanation"
        else:
            profile = self.learner.profile(session.learner_id)
            sym = f"\nThey specifically asked about the symbol `{symbol}`." if symbol else ""
            analysis = analyze(code)
            text, via = self.llm.chat_with_failover(
                system=prompts.WHY_SYSTEM,
                user=prompts.WHY_USER.format(
                    profile=profile.as_prompt_block(), code=code,
                    facts=facts_for_prompt(analysis), line=line, text=text_line, symbol=sym),
                models=self._strong_chain(session),
            )
            if not response_is_grounded(text, analysis, automatic=False):
                text = (f"I can't verify the model's explanation for line {line}, so I won't "
                        "present it as fact. This line is `{text_line}`. What value or action "
                        "do you expect it to provide to the next line?")
                via = "safety check"
            text = beginnerize(text)
        return MentorMessage(kind="why", text=text, line=line, via=via)

    def fix_line(self, session: Session, code: str, line: int) -> MentorMessage:
        """Return a previewable one-line repair; never called by automatic tutoring."""
        original = _raw_line_at(code, line)
        if not original:
            return MentorMessage(kind="fix", text="That line is empty, so there is nothing to replace.", line=line)
        replacement, explanation = _deterministic_line_fix(original)
        via = "Python-aware fix"
        analysis_before = analyze(code)
        issue_on_line = next((issue for issue in analysis_before.context_issues
                              if issue.line == line and not issue.ask_intent), None)
        syntax_on_line = bool(analysis_before.syntax_issue and analysis_before.syntax_issue.line == line)
        if replacement is None and not issue_on_line and not syntax_on_line:
            if analysis_before.syntax_issue:
                issue = analysis_before.syntax_issue
                return MentorMessage(kind="fix", text=(
                    f"Line {line} is not the parser's verified failure, so I will not rewrite it. "
                    f"Python first reports line {issue.line}: {issue.message}. Fix that line, then check this one again."
                ), line=line, via="Python-aware fix")
            return MentorMessage(kind="fix", text=(
                f"Python considers line {line} structurally valid, and I do not have a verified "
                "one-line correction for it. I will not ask the model to invent a replacement."
            ), line=line, via="Python-aware fix")
        if replacement is None and issue_on_line:
            return MentorMessage(kind="fix", text=issue_on_line.explanation,
                                 line=line, via="Python-aware fix")
        if replacement is None and not self.llm.offline:
            raw, via = self.llm.chat_with_failover(
                system=prompts.FIX_SYSTEM,
                user=prompts.FIX_USER.format(code=code, line=line, text=original),
                models=self._strong_chain(session), max_tokens=250,
            )
            replacement, explanation = _parse_fix(raw, original)
        if replacement is None:
            return MentorMessage(kind="fix", text=(explanation or
                "I cannot verify a safe one-line correction. Ask me to explain the error first."), line=line)
        candidate_lines = code.splitlines()
        candidate_lines[line - 1] = replacement
        candidate = analyze("\n".join(candidate_lines))
        if candidate.syntax_issue and candidate.syntax_issue.line == line:
            return MentorMessage(kind="fix", text=(
                "I rejected the proposed replacement because Python still reports a problem "
                "on that line. I won't apply an unverified fix."), line=line, via="safety check")
        return MentorMessage(kind="fix", text=beginnerize(explanation), line=line,
                             replacement=replacement, via=via,
                             headline="Review this proposed line fix")


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


def _raw_line_at(code: str, line: int) -> str:
    lines = code.splitlines()
    return lines[line - 1] if 1 <= line <= len(lines) else ""


def _deterministic_line_fix(line: str) -> tuple[Optional[str], str]:
    indent = line[:len(line) - len(line.lstrip())]
    body = line.strip()
    if body.startswith("improt "):
        fixed = indent + "import " + body[len("improt "):]
        return fixed, ("`improt` is a spelling error. Python recognizes the exact keyword "
                       "`import`, which loads a library so its tools can be used.")
    if re.match(r"^(if|elif|for|while|def|class)\b", body) and not body.endswith(":"):
        return line.rstrip() + ":", ("This line opens an indented block. Python uses the final "
                                     "colon to mark where that block begins.")
    return None, ""


def _parse_fix(raw: str, original: str) -> tuple[Optional[str], str]:
    replacement = re.search(r"(?m)^REPLACEMENT:\s*(.+)$", raw)
    explanation = re.search(r"(?ms)^EXPLANATION:\s*(.+)$", raw)
    if not replacement or not explanation:
        return None, "The model did not return a verifiable one-line correction."
    line = replacement.group(1).rstrip()
    if not line or "\n" in line or line == original:
        return None, "The proposed correction was empty, unchanged, or exceeded one line."
    return line, explanation.group(1).strip()


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


def _blueprint_quality_problems(steps: List[str], level: str) -> List[str]:
    """Validate teaching usefulness, not the model's preferred wording."""
    problems: List[str] = []
    if not 4 <= len(steps) <= 8:
        problems.append("use 4 to 8 ordered steps")
    joined = " ".join(steps).lower()
    if level == "beginner":
        concrete = re.compile(
            r"`[^`]+`|\b(?:input\(\)|print\(\)|int\(\)|float\(\)|import|function|def |"
            r"if\b|elif\b|else\b|loop|list|dict|return|variable|class)"
        )
        concrete_steps = sum(bool(concrete.search(step.lower())) for step in steps)
        reasons = sum(any(word in step.lower() for word in ("because", " so ", " so that ", "why"))
                      for step in steps)
        if concrete_steps < max(3, len(steps) // 2):
            problems.append("name the exact Python construct or small code fragment in most steps")
        if reasons < 2:
            problems.append("explain why each early action is needed")
        if "import" not in joined:
            problems.append("state whether an import is needed and name it when it is")
        if any(phrase in joined for phrase in (
                "clarify what", "choose a simple structure", "represent the information")):
            problems.append("replace abstract planning language with instructions a beginner can type")
    elif level == "intermediate":
        if not any(word in joined for word in ("validate", "test", "trade-off", "choice", "error")):
            problems.append("include an implementation choice plus validation or testing")
    elif level == "advanced":
        if not any(word in joined for word in (
                "architecture", "boundary", "trade-off", "failure", "security", "scal", "observ")):
            problems.append("include architecture boundaries, trade-offs, or operational failure modes")
    return problems


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

def _is_age_ticket_project(goal: str) -> bool:
    low = goal.lower()
    return "age" in low and "ticket" in low and any(
        word in low for word in ("price", "pricing", "cost"))


def _has_guided_beginner_blueprint(goal: str) -> bool:
    low = goal.lower()
    return (_is_temperature_converter(low) or _is_age_ticket_project(low)
            or any(term in low for term in ("to-do", "todo", "task list")))


def _beginner_composition_hint(session: Session, a: Analysis, level: int) -> str:
    """Goal-aware rescue for a beginner who pauses while composing a function."""
    if session.learner_level != "beginner":
        return ""
    source, last = a.source, a.last_line.strip()
    if last.startswith("def ") and not last.endswith(":"):
        if _is_age_ticket_project(session.goal):
            return ("Complete the function header as `def ticket_price(age):`. `age` is the "
                    "number the function will receive, and the final `:` tells Python that "
                    "an indented function body comes next. No import is needed.")
        return ("A function header needs a name, parentheses for its inputs, and a final `:`. "
                "Finish that header first; the next indented line will describe its work.")
    if not _is_age_ticket_project(session.goal):
        return ""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""
    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
    if not functions:
        return ("Start with `def ticket_price(age):`. This gives the pricing decision one "
                "clear home; `age` is the value it will examine. No import is required.")
    fn = functions[0]
    has_branch = any(isinstance(node, ast.If) for node in ast.walk(fn))
    has_return = any(isinstance(node, ast.Return) for node in ast.walk(fn))
    if not has_branch:
        return ("On the next indented line, start the first age-band decision with `if`. "
                "Use the first agreed boundary and end the condition with `:` because the "
                "price assignment beneath it belongs to that case.")
    if not has_return:
        return ("Your branches choose a price, but the answer is still trapped inside the "
                "function. Add `return price` once, aligned with the `if`, so every branch's "
                "chosen value flows back to the caller.")
    calls = list(ast.walk(tree))
    has_input = any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "input" for node in calls)
    if not has_input:
        return ("The pricing function is ready to receive an age. Below the function, collect "
                "one with `age = int(input(\"Enter age: \"))`; `input()` gives text, and "
                "`int()` turns that text into a number the comparisons can use.")
    return ("Now call the pricing function with the collected age and show its returned value, "
            "for example by passing the call to `print()`. This connects input → decision → output.")

def _offline_blueprint(goal: str, level: str = "beginner") -> List[str]:
    low = goal.lower()
    if _is_age_ticket_project(low):
        if level == "advanced":
            return ["Define pricing-policy ownership, effective dates, and audit requirements",
                    "Separate the pricing domain rule from input/output and persistence",
                    "Model boundary inclusivity explicitly and prevent overlapping bands",
                    "Add table-driven tests, observability, and a policy-evolution strategy"]
        if level == "intermediate":
            return ["Define the age bands and inclusive boundaries before implementing them",
                    "Encapsulate the pricing rule in one focused function",
                    "Validate and convert user input at the program boundary",
                    "Test every boundary plus invalid and negative ages"]
        return [
            "No import is needed. Define `ticket_price(age)` so one function owns the pricing decision",
            "Inside the function, use `if` / `elif` / `else` for the agreed age bands because only one price should win",
            "Return the selected `price` so code outside the function can use the answer",
            "Below the function, ask for the age with `input()` and convert it with `int()` because input starts as text",
            "Call `ticket_price(age)` and pass its returned value to `print()` so the learner can see the price",
            "Test an age inside each band and exactly on every boundary so the conditions are verified",
        ]
    if any(term in low for term in ("to-do", "todo", "task list")):
        if level == "advanced":
            return ["Define the domain boundary and persistence requirements",
                    "Choose functions, classes, or services and justify the trade-off",
                    "Separate interface, task operations, and storage responsibilities",
                    "Design error handling, tests, and an evolution path to persistence"]
        if level == "intermediate":
            return ["Choose an in-memory task representation and explain the trade-off",
                    "Encapsulate add and display behavior with focused functions",
                    "Build a command loop with input validation",
                    "Test normal, empty, and invalid-input paths"]
        return ["Create `tasks = []`, an empty list that will remember the tasks",
                "Add one task to the list and confirm it was stored",
                "Display every saved task clearly",
                "Let the user choose whether to add, view, or exit",
                "Repeat the menu until the user chooses to exit",
                "Test adding, viewing, and exiting with simple inputs"]
    if _is_temperature_converter(low):
        if level == "advanced":
            return ["Define supported units, precision, and invalid-input behavior",
                    "Separate conversion rules from input/output boundaries",
                    "Model extensibility beyond Celsius and Fahrenheit",
                    "Add property-based tests for inverse conversions and edge cases"]
        if level == "intermediate":
            return ["Read a numeric temperature and validate the input",
                    "Implement focused Celsius-to-Fahrenheit and reverse functions",
                    "Let the user select the conversion direction",
                    "Test freezing, boiling, negative, and invalid values"]
        return ["Use `input()` to ask for the temperature; no import is needed",
                "Convert the typed text to a number with `float()` so calculations work",
                "Ask whether the value is Celsius or Fahrenheit",
                "Apply the matching conversion formula and store the result",
                "Use `print()` to show the converted value with its unit",
                "Test 0°C → 32°F and 32°F → 0°C"]
    return [
        f"Clarify what '{goal}' must accept and produce — the plan follows that information flow.",
        "Choose a simple structure only after comparing the valid options — functions and classes teach different ideas.",
        "Represent the information the program must remember — later actions need a reliable place to find it.",
        "Add one behavior at a time and exercise it immediately — small checks reveal misunderstandings early.",
        "Connect the behaviors into the smallest complete journey — this proves the pieces work together.",
        "Test a normal case and one awkward case — useful programs behave clearly beyond the happy path.",
    ]


def _offline_next_step(a: Analysis, session: Session, profile=None) -> str:
    line = a.last_line.strip()
    goal = session.goal.lower()
    if session.learner_level == "beginner" and any(x in goal for x in ("to-do", "todo", "task list")):
        if line.startswith("import"):
            return ("A basic to-do list does not need `sys` or another imported library yet. "
                    "Remove that import, then create `tasks = []`. The empty list gives the "
                    "program one place to remember every task the user adds.")
        if "tasks" not in a.source:
            return ("Next, create `tasks = []`. This empty list is the program's memory for "
                    "the tasks the user will add.")
    if session.learner_level == "beginner" and _is_temperature_converter(goal):
        if line.startswith("import"):
            return ("This temperature converter does not need a library. Remove the import, "
                    "then start with `temperature = input(\"Enter a temperature: \")`. "
                    "`input()` pauses the program and captures what the user types.")
        return _beginner_step_message(session)
    # Demonstrate learner-model filtering even in offline mode.
    if profile is not None and session.learner_level != "beginner":
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


def _offline_stuck(a: Analysis, session: Session, level: int = 1) -> str:
    if a.condition_hint:
        return a.condition_hint
    line = a.last_line.strip()
    if session.learner_level == "beginner" and any(
            term in session.goal.lower() for term in ("to-do", "todo", "task list")):
        step = session.current_step_text()
        if level == 1:
            return f"Do this next: {step}\nWhy: it is the next unfinished part of your to-do-list plan."
        if level == 2:
            return (f"Focus only on this step: {step} Start with the smallest line that makes "
                    "that behavior possible; do not rewrite the parts already working.")
        return (f"Your earlier steps are already present, so leave them in place. Current step: "
                f"{step} If you are unsure, ask me about that step and I will give one small "
                "to-do-list example rather than a different program template.")
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
    if level == 1:
        if session.learner_level == "beginner":
            return _beginner_step_message(session)
        if session.learner_level == "advanced":
            return (f"Current design step: {session.current_step_text()}. Before implementing, "
                    "make the boundary, failure modes, and test strategy explicit.")
        return ("Taking a pause? What should the part you just created make possible next?")
    if level == 2:
        return ("The next step should give one clear job a name, store information the program "
                "needs, or use the value you just produced. Which of those matches your plan?")
    if level == 3:
        return ("Choose the smallest next responsibility and describe its input and result in "
                "plain words before writing it. That tells you whether it belongs in a function, "
                "a class, or one simple statement.")
    return ("Use this as a shape, not a finished answer:\n```python\n"
            "def one_clear_action(input_value):\n    # transform or use input_value\n"
            "    return result\n```\nRename each part for your goal and write only the first line you understand.")


def _import_pause_guidance(goal: str, level: int = 1,
                           learner_level: str = "beginner") -> str:
    """Suggest a conservative library only when the learner's goal supports it."""
    goal_words = goal.lower()
    if any(term in goal_words for term in ("to-do", "todo", "task list")):
        if learner_level == "beginner":
            return ("A basic to-do list does not need a library import yet. Delete `import`, "
                    "then write `tasks = []`. The list will store each task the user adds.")
        return ("No import is required for an in-memory to-do list. Use Python's built-in list "
                "unless your design specifically requires persistence, a framework, or an API.")
    if _is_temperature_converter(goal_words):
        return ("A temperature converter does not need a library. Delete `import`, then start "
                "with `temperature = input(\"Enter a temperature: \")`. `input()` is built into "
                "Python and collects the value the user wants to convert.")
    if any(word in goal_words for word in ("weather", "api", "http", "website", "web service")):
        base = ("For this goal, `requests` is a common choice. It lets Python ask a website "
                "or API for information—like sending a question and receiving a response. "
                "Try `import requests`.")
        if level >= 2:
            base += (" It is an add-on library, so install it with `pip install requests` "
                     "if Python says it cannot find it.")
        return base
    if any(word in goal_words for word in ("machine learning", "prediction", "classification", "regression model")):
        return ("For a first machine-learning project, `scikit-learn` is a common toolkit. "
                "It provides ready-made learning algorithms and evaluation tools, so you can "
                "focus on understanding the workflow. Before importing anything, which part "
                "comes first for your goal: loading data, exploring it, or training a model?")
    if any(word in goal_words for word in ("csv", "spreadsheet", "table", "data analysis")):
        return ("For table-shaped data, `pandas` is a common choice. It helps Python load and "
                "work with rows and columns, much like a programmable spreadsheet. Is your "
                "first input a CSV file, an Excel file, or something else?")
    if any(word in goal_words for word in ("chart", "graph", "plot", "visualization")):
        return ("For a basic chart, `matplotlib.pyplot` is a common choice. It gives Python "
                "drawing tools for plots and graphs. What are you trying to show: a change "
                "over time, a comparison, or a relationship between values?")
    return ("An import brings in a tool that Python does not load automatically. I don't want "
            "to guess the library from the program name alone: what capability do you need "
            "next—reading a file, calling an API, analyzing data, drawing a chart, or something else?")


def _update_current_step(session: Session, a: Analysis) -> None:
    low_goal, source = session.goal.lower(), a.source.lower()
    if not session.blueprint:
        return
    if _is_age_ticket_project(low_goal):
        if not re.search(r"\bdef\s+\w+\s*\(", source):
            step = 0
        elif not re.search(r"\b(?:if|elif)\b", source):
            step = 1
        elif "return" not in source:
            step = 2
        elif "input(" not in source or not re.search(r"\bint\s*\(", source):
            step = 3
        elif not re.search(r"print\s*\([^\n]*(?:ticket|price)\w*\s*\(", source):
            step = 4
        else:
            step = 5
        session.current_step = min(step, len(session.blueprint) - 1)
        return
    if _is_temperature_converter(low_goal):
        tree = a.tree if hasattr(a, "tree") else None
        try:
            tree = ast.parse(a.source)
        except SyntaxError:
            tree = None
        calls = list(ast.walk(tree)) if tree is not None else []
        has_input = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                        and n.func.id == "input" for n in calls)
        has_numeric = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                          and n.func.id in {"float", "int"} for n in calls)
        has_unit_input = sum(1 for n in calls if isinstance(n, ast.Call)
                             and isinstance(n.func, ast.Name) and n.func.id == "input") >= 2
        has_normalized_unit = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                                  and n.func.attr in {"upper", "lower"} for n in calls)
        has_c_formula = bool(re.search(r"celsius\s*\*\s*(?:9\s*/\s*5|1\.8).{0,20}\+\s*32", source))
        has_f_formula = bool(re.search(r"\(?(?:fahrenheit|temp\w*)\s*-\s*32\)?.{0,20}\*\s*(?:5\s*/\s*9|0\.5{2,})", source))
        conversion_names = {"celsius_to_fahrenheit", "fahrenheit_to_celsius"}
        uses_result = False
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) \
                        and isinstance(node.value.func, ast.Name) \
                        and node.value.func.id in conversion_names:
                    uses_result = True
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                        and node.func.id == "print":
                    if any(isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
                           and child.func.id in conversion_names for child in ast.walk(node)):
                        uses_result = True
                    printed_names = {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}
                    assigned_results = {
                        target.id for assign in ast.walk(tree) if isinstance(assign, ast.Assign)
                        and isinstance(assign.value, ast.Call) and isinstance(assign.value.func, ast.Name)
                        and assign.value.func.id in conversion_names
                        for target in assign.targets if isinstance(target, ast.Name)
                    }
                    if printed_names & assigned_results:
                        uses_result = True
        if not has_input:
            step = 0
        elif not has_numeric:
            step = 1
        elif not (has_unit_input and has_normalized_unit):
            step = 2
        elif not (has_c_formula and has_f_formula):
            step = 3
        elif not uses_result:
            step = 4
        else:
            step = 5
        session.current_step = min(step, len(session.blueprint) - 1)
        return
    if not any(term in low_goal for term in ("to-do", "todo", "task list")):
        return
    if "tasks" not in source:
        step = 0
    elif not any(name in source for name in ("append(", "add_task", "addtask")):
        step = 1
    elif not any(name in source for name in ("print(", "display", "show_task", "view_task")):
        step = 2
    elif not any(name in source for name in ("input(", "while ", "for ")):
        step = 3
    else:
        step = 4
    session.current_step = min(step, len(session.blueprint) - 1)


def _is_temperature_converter(goal: str) -> bool:
    low = goal.lower()
    has_temperature = any(word in low for word in ("temperature", "celsius", "fahrenheit"))
    has_conversion = any(word in low for word in ("convert", "converter", "celsius", "fahrenheit"))
    return has_temperature and has_conversion


def _beginner_step_message(session: Session) -> str:
    step = session.current_step_text()
    watch = "Write only this step before moving on; I will check it against the program goal."
    if "input()" in step:
        watch = "`input()` returns text, so the next step will convert that text before doing arithmetic."
    elif "float()" in step:
        watch = "Keep the converted number in a clearly named variable so both formulas can use it."
    elif "Celsius or Fahrenheit" in step:
        watch = "Call `.upper()` with parentheses; `.upper` by itself is only the method, not its result."
    elif "formula" in step:
        watch = "Each function should return its result; do not print inside the formula function yet."
    elif "print()" in step:
        watch = "Store the returned conversion or pass the function call directly to `print()`."
    return f"Do this next: {step}.\nWhy: it is the next verified piece needed for `{session.goal}`.\nWatch for: {watch}"


def _beginner_issue_message(issue: ContextIssue) -> str:
    if issue.code == "method_reference_not_called":
        action = "Add `()` after the text method on this comparison line—for example, use `.upper()` instead of `.upper`."
        watch = "A method name without parentheses is the tool itself; parentheses run it and produce text."
    elif issue.code == "discarded_return_value":
        action = "Store this function call in a variable or put the call inside `print()` so its returned value is used."
        watch = "Calling a function does not automatically display the value it returns."
    elif issue.code == "input_outside_loop":
        action = "Move the menu's `input(...)` line inside `while True`, immediately before the `if` branches."
        watch = "Code outside a loop runs only once; code inside it can collect a fresh choice each repetition."
    elif issue.code == "prompt_string_used_as_value":
        action = "Put the question inside `input(...)` so the variable stores what the learner types."
        watch = "A quoted question is only text; `input(\"question\")` displays it and captures the answer."
    else:
        action = f"Correct line {issue.line}: {issue.summary}."
        watch = issue.explanation
    return f"Do this next: {action}\nWhy: {issue.explanation}\nWatch for: {watch}"


def _goal_aware_beginner_answer(session: Session, question: str, code: str) -> str:
    if session.learner_level != "beginner":
        return ""
    goal, q = session.goal.lower(), question.lower()
    if not any(term in goal for term in ("to-do", "todo", "task list")):
        return ""
    if any(term in q for term in ("library", "import", "module")):
        return ("You do not need a library for the first version of this to-do list. "
                "Python's built-in list can store the tasks. Remove the import and write "
                "`tasks = []`; the square brackets create an empty list ready to remember tasks.")
    if any(term in q for term in ("next", "step", "blueprint", "what should")):
        analysis = analyze(code)
        _update_current_step(session, analysis)
        return (f"Do this next: {session.current_step_text()}. This step is needed before the "
                "later behavior has reliable data to work with. Try that one step, then I'll "
                "check it and guide you forward.")
    return ""


def _implicit_none_answer(session: Session, question: str, code: str) -> str:
    """Explain a likely fall-through return from Python evidence, for any lesson."""
    q = question.lower()
    describes_none = ("none" in q and any(term in q for term in (
        "return", "respond", "result", "output", "get", "print", "why", "shows", "showing"
    ))) or any(term in q for term in (
        "no keyword", "no match", "nothing match", "doesn't match", "does not match"
    ))
    if not describes_none:
        return ""
    try:
        ast.parse(code)
    except SyntaxError:
        return "Fix the current syntax error first; then we can add the no-match behavior safely."
    functions = possible_implicit_none_functions(code, require_consumed=False)
    if not functions:
        return ""
    function = functions[0]
    if ("support-ticket classifier" in session.goal.lower()
            or "support ticket classifier" in session.goal.lower()):
        return ("Exactly: `None` means the function reached its end without executing a `return`. "
                f"The `{function}` function is not complete yet. After the category/keyword loop—"
                "not inside it—add `return \"uncategorized\"`. That guarantees every ticket "
                "receives a clear result. Then rerun it with text that matches no keyword.")
    return (f"`None` means `{function}` reached its end on at least one path without returning a "
            "value. Check its conditions and loops: after that decision logic, at the function's "
            "normal indentation, add an explicit fallback `return` that makes sense for this "
            "program. Then test both a path that matches and one that does not.")


def _deterministic_diagnostic_answer(session: Session, question: str, code: str) -> str:
    """Answer explicit diagnostic questions from parser evidence before consulting an LLM."""
    q = question.lower()
    if not any(term in q for term in (
            "what's wrong", "what is wrong", "anything wrong", "error", "problem",
            "why doesn't", "why does not", "not working")):
        return ""
    if not code.strip():
        return ("I do not currently have a Python buffer to inspect. Return focus to the file "
                "once, then ask again; I will not pretend an unseen file is empty or correct.")
    analysis = analyze(code)
    if analysis.syntax_issue:
        issue = analysis.syntax_issue
        return (f"Python cannot parse line {issue.line}: `{issue.text.strip()}`. "
                f"{issue.message}. Fix this syntax problem first, then I can evaluate the behavior below it.")
    corrections = [issue for issue in analysis.context_issues if not issue.ask_intent]
    if corrections:
        issue = corrections[0]
        return (f"The first verified problem is on line {issue.line}: {issue.summary}. "
                f"{issue.explanation}")
    _update_current_step(session, analysis)
    return ("I do not see a parser-proven error in the current file. That does not guarantee "
            f"the behavior is correct. Your next unchecked lesson step is: {session.current_step_text()}")


def _explicit_guided_subgoal_answer(session: Session, question: str, code: str) -> str:
    """Turn an explicit Beginner question into a persistent, observable subgoal."""
    if session.learner_level != "beginner" or not _is_temperature_converter(session.goal):
        return ""
    q = question.lower()
    asks_function = any(word in q for word in ("function", "method", "convert"))
    c_to_f = any(word in q for word in ("celsius", "fahrenheit", "conversion"))
    if not (asks_function and c_to_f):
        return ""
    session.guided_subgoal = "celsius_to_fahrenheit"
    return (
        "A function receives a value, performs one job, and returns the result. Here is a "
        "similar shape—not your answer:\n```python\n"
        "def double(number):\n    return number * 2\n```\n"
        "Now try writing only `def celsius_to_fahrenheit(celsius):`. Pause there if you "
        "want me to help you build its body one step at a time."
    )


def _guided_subgoal_hint(session: Session, a: Analysis, level: int) -> str:
    if session.guided_subgoal != "celsius_to_fahrenheit":
        return ""
    source = a.source
    signature = re.search(r"(?m)^\s*def\s+celsius_to_fahrenheit\s*\(\s*celsius\s*\)\s*:\s*$", source)
    correct_formula = re.search(
        r"(?m)^\s*return\s+\(?\s*celsius\s*\*\s*(?:9\s*/\s*5|1\.8)\s*\)?\s*\+\s*32\s*$",
        source,
    )
    if correct_formula:
        session.guided_subgoal = ""
        return ("That function now returns the Celsius-to-Fahrenheit calculation. Next, call "
                "it with the number the user entered and store or print the returned value.")
    if not signature:
        return ("Start the requested function with `def celsius_to_fahrenheit(celsius):`. "
                "The name describes its job, and `celsius` is the value it will receive.")
    if level <= 1:
        return ("Inside this function, add one indented `return` line. It should transform "
                "the received `celsius` value and send the Fahrenheit result back.")
    if level == 2:
        return ("Try this next inside the function: multiply `celsius` by 9, divide by 5, "
                "then add 32. Put that calculation after an indented `return`.")
    return ("Try this next:\n```python\n    return (celsius * 9 / 5) + 32\n```\n"
            "The multiplication changes the size of each degree; adding 32 aligns the two "
            "scales at water's freezing point.")


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


def _deterministic_why(code: str, line_no: int, symbol: Optional[str]) -> str:
    """Explain common Python lines from their AST; never invent downstream behavior."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ""
    node = next((n for n in ast.walk(tree) if getattr(n, "lineno", -1) == line_no), None)
    line = _line_at(code, line_no)
    if isinstance(node, ast.Assign):
        names = [n.id for target in node.targets for n in ast.walk(target) if isinstance(n, ast.Name)]
        name = names[0] if names else "this name"
        if isinstance(node.value, ast.List):
            return (f"`{line}` creates a list and stores it under `{name}`. An empty list is a "
                    "place the program can add related values later; here, it can remember tasks.")
        return (f"`{line}` calculates or receives a value and stores it under `{name}` so later "
                "lines can refer to that value by name.")
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = ", ".join(a.arg for a in node.args.args) or "no input values"
        return (f"This defines `{node.name}()` as a reusable action. It receives {args}; the "
                "indented lines below describe what happens when the function is called.")
    if isinstance(node, ast.Return):
        return "`return` sends a result back to the line that called this function; it does not display the value by itself."
    if isinstance(node, ast.While):
        return "This `while` line repeats its indented block while the condition remains true; a `break` can stop it."
    if isinstance(node, ast.If):
        return "This `if` line asks a yes-or-no question. Python runs its indented block only when that comparison is true."
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        call = node.value
        name = call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", "function")
        if name == "print":
            return "`print(...)` displays the value inside the parentheses so the learner or user can see it."
        return f"This line calls `{name}()` now. Python enters that function, passes these arguments, and then continues here."
    return ""


def _offline_why(line: str, symbol: Optional[str]) -> str:
    focus = f"`{symbol}` in " if symbol else ""
    return (f"This line {focus}`{line}` is valid Python, but I cannot prove its role from "
            "the syntax alone. Select the value or action you want to understand, or ask "
            "what changes if this line is removed; I will avoid guessing.")


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
