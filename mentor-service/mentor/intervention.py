"""Model-independent decisions about when and how CodeTutor should intervene.

The language model may phrase a teaching response, but it must not decide whether a
learner is progressing, thinking, stuck, off-path, or facing a verified error.  That
decision is derived from parser facts, edit progress, idle time, and the lesson state.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import List

from .analyzer import Analysis


@dataclass(frozen=True)
class InterventionDecision:
    learner_state: str
    confidence: float
    evidence: List[str]
    should_intervene: bool
    hint_level: int
    actions: List[str]
    intervention_id: str

    def to_dict(self) -> dict:
        return asdict(self)


def decide_intervention(
    analysis: Analysis,
    *,
    code_changed: bool,
    idle_seconds: int,
    hint_level: int,
    learner_level: str,
    event: str = "stuck",
) -> InterventionDecision:
    """Classify the learner state using only observable, deterministic evidence."""
    evidence: List[str] = []
    state = "thinking"
    confidence = 0.55
    should_intervene = event != "completed"

    issue = analysis.syntax_issue
    issue_text = issue.message.lower() if issue else ""
    transient_block = bool(
        issue and "expected an indented block" in issue_text
        and analysis.last_line.strip().endswith(":")
    )

    if issue and not transient_block:
        state, confidence = "error", 0.99
        evidence.append(f"Python parser: {issue.message}")
    elif any(not item.ask_intent for item in analysis.context_issues):
        state, confidence = "off_path", 0.96
        evidence.append("Python structure shows a high-confidence placement or usage issue")
    elif analysis.last_line.strip() in {"import", "from"}:
        state, confidence = "stuck", 0.94
        evidence.append("A library statement was started but no library was selected")
    elif not analysis.last_line_complete or analysis.condition_hint:
        state, confidence = "stuck", 0.93
        evidence.append("The current statement is incomplete")
    elif event == "completed" or (code_changed and idle_seconds < 8):
        state, confidence, should_intervene = "progressing", 0.88, False
        evidence.append("The learner has just made a meaningful edit")
    elif not analysis.source.strip():
        if learner_level == "beginner":
            state, confidence = "stuck", 0.82
            evidence.append("The beginner lesson has not started after the idle interval")
        else:
            state, confidence = "thinking", 0.58
            evidence.append("The file is empty, but no broken construct identifies the obstacle")
    elif not code_changed and hint_level > 1:
        state, confidence = "stuck", min(0.96, 0.72 + hint_level * 0.06)
        evidence.append("The code is unchanged after earlier guidance")
    elif learner_level == "beginner" and idle_seconds >= 8:
        state, confidence = "stuck", 0.76
        evidence.append("The learner paused beyond the configured interval during a guided lesson")
    else:
        state, confidence = "thinking", 0.61
        evidence.append("The pause is real, but the code contains no verified obstacle")

    actions = ["another_hint", "similar_example", "wait"]
    if state in {"error", "off_path"} and issue:
        actions.append("fix_and_explain")

    digest = hashlib.sha256(
        f"{state}|{hint_level}|{analysis.source}|{analysis.last_line}".encode("utf-8")
    ).hexdigest()[:12]
    return InterventionDecision(
        learner_state=state,
        confidence=round(confidence, 2),
        evidence=evidence,
        should_intervene=should_intervene,
        hint_level=hint_level,
        actions=actions,
        intervention_id=digest,
    )
