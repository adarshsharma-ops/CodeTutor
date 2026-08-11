"""Safety rails between probabilistic model output and a beginner.

The parser owns facts. The model may explain those facts, but automatic tutoring never
gets to invent a diagnosis or jump straight to a completed solution.
"""
from __future__ import annotations

import re
from typing import Optional

from .analyzer import Analysis, ContextIssue


_JARGON = {
    "method signature": "method heading (the `def` line that names its inputs)",
    "instance variable": "value stored on this particular object",
    "instance": "object created from a class",
    "parameter": "input name",
    "iterable": "something Python can go through one item at a time",
}


def beginnerize(text: str) -> str:
    """Define common terms at first use without changing code tokens."""
    out = text.strip()
    for term, plain in _JARGON.items():
        out = re.sub(rf"\b{re.escape(term)}\b", plain, out, count=1, flags=re.I)
    return out


def facts_for_prompt(analysis: Analysis) -> str:
    if not analysis.verified_facts:
        return "- No additional structural fact was proven by the parser."
    return "\n".join(f"- {fact}" for fact in analysis.verified_facts)


def hint_policy(level: int) -> str:
    policies = {
        1: "Ask one guiding question. Do not show code.",
        2: "Name the concept and the relevant line, then ask what should happen. Do not show code.",
        3: "Describe the smallest change in words and explain why. Do not provide a full body.",
        4: "Give one tiny analogous example, then return to their code. Never provide the whole solution.",
    }
    return policies.get(level, policies[1])


def response_is_grounded(text: str, analysis: Analysis, *, automatic: bool,
                         level: int = 1) -> bool:
    low = text.lower()
    issue_codes = {i.code for i in analysis.context_issues}
    if automatic and level < 4 and ("```" in text or re.search(r"(?m)^\s*(def|class)\s+\w+", text)):
        return False
    # A model cannot diagnose an undefined/early-read variable unless the analyzer did.
    if any(p in low for p in ("used before", "before it has a value", "is not defined", "undefined")):
        if "local_used_before_assignment" not in issue_codes:
            return False
    has_class = any(f.startswith("class `") for f in analysis.verified_facts)
    if not has_class and any(p in low for p in ("instance of", "self parameter", "method signature")):
        return False
    if automatic and len(text) > 850:
        return False
    return True


def safe_automatic_fallback(kind: str, analysis: Analysis, level: int,
                            issue: Optional[ContextIssue] = None) -> str:
    """A short parser-grounded hint used when a local/remote model is unsafe."""
    if issue:
        if level == 1 or issue.ask_intent:
            return f"Look at line {issue.line}: {issue.summary}. {issue.explanation} What did you intend this part to do?"
        if level == 2:
            return f"Focus on line {issue.line}. {issue.explanation} Which indentation level would give it the job you intended?"
        return f"Make the smallest placement change around line {issue.line}. {issue.explanation} Then check whether the object can call it where you use it."
    if kind == "stuck":
        if analysis.condition_hint:
            return analysis.condition_hint
        line = analysis.last_line.strip()
        if not analysis.last_line_complete:
            if level == 1:
                return "What is the one missing piece this unfinished line still needs?"
            return "Read the line from left to right and name what Python has received so far. What must come next to complete that instruction?"
        return "You may still be thinking. What should the value you just created be used for next?"
    if analysis.last_line.strip().endswith(":"):
        return "This line opens a block. What is the first action that should happen inside it, and why?"
    return "Before adding more code, say in one sentence what the last line produced and which next step needs that result."


def advice_key(kind: str, analysis: Analysis, issue: Optional[ContextIssue] = None) -> str:
    if issue:
        return f"{kind}:{issue.code}:{issue.line}"
    normalized = re.sub(r"[A-Za-z_]\w*", "name", analysis.last_line.strip().lower())
    return f"{kind}:{normalized[:100]}"
