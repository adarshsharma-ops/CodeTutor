#!/usr/bin/env python3
"""Evaluation harness — judge the mentor's teaching QUALITY across scenarios.

This is the tool for answering "is this actually good?". It runs a battery of
scenarios covering every behavior (blueprint, next-step, error, stuck, explain, why),
prints each prompt/response, and writes a Markdown report you can read critically.

Usage:
    python eval_harness.py                 # offline baseline
    python eval_harness.py --out report.md # also write a Markdown report

    # Against a real model — THE point of this harness:
    export OPENAI_API_KEY=...  OPENAI_BASE_URL=https://.../v1  MENTOR_MODEL=gpt-4o-mini
    python eval_harness.py --out report_llm.md

Read the report and ask, per response:
  * Is the reasoning genuinely insightful, or generic filler?
  * Right length — a nudge, not a lecture?
  * Does the "recurring misconception" reframe actually teach differently?
  * Does "why is this here?" answer what/why/what-breaks specifically?
Then tune prompts.py accordingly.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import List, Tuple

os.environ.setdefault("MENTOR_LEARNER_DB", os.path.join(os.path.dirname(__file__), "eval_learner.db"))

from mentor.config import Config
from mentor.llm import LLMClient
from mentor.learner_model import LearnerModel
from mentor.mentor import Mentor
from mentor.state import Session, SessionStore

WEATHER = "Build a small weather app that fetches the temperature for a city"

# Each step: (label, callable(mentor, session) -> MentorMessage or None)
Scenario = Tuple[str, List[Tuple[str, str, dict]]]

# A step is (behavior, code, kwargs). behavior in:
#   completed | error | stuck | explain | why
SCENARIOS: List[Scenario] = [
    ("Weather app — next-step hints + first-use curiosity", [
        ("completed", "import requests", {}),
        ("completed", "import requests\nurl = \"https://api.example.com?city=London\"", {}),
        ("completed", "import requests\nurl = \"https://api.example.com?city=London\"\nr = requests.get(url)", {}),
        ("completed", "import requests\nurl = \"https://api.example.com?city=London\"\nr = requests.get(url)\ndata = r.json()", {}),
    ]),
    ("Curiosity payoff — learner says YES to an explanation", [
        ("explain", "import requests", {"target": "requests"}),
        ("explain", "data = r.json()", {"target": "json()"}),
    ]),
    ("Why is this here? — line-specific reasoning", [
        ("why", "import requests\nurl = \"https://api.example.com?city=London\"\nr = requests.get(url)\ndata = r.json()", {"line": 1, "symbol": "requests"}),
        ("why", "import requests\nurl = \"https://api.example.com?city=London\"\nr = requests.get(url)\ndata = r.json()", {"line": 4, "symbol": "json"}),
    ]),
    ("Stuck — unfinished lines", [
        ("stuck", "cities = [\"London\", \"Paris\"]\nfor i in ", {}),
        ("stuck", "total = ", {}),
        ("stuck", "data = r.", {}),
    ]),
    ("Stuck — choosing a condition operator", [
        ("stuck", "minimum_age = 18\nif age", {}),
        ("stuck", "minimum_age = 18\nif age >=", {}),
        ("stuck", "if adult and", {}),
        ("stuck", "if role in", {}),
    ]),
    ("Recurring misconception — same typo 3x should change strategy", [
        ("error", "improt requests", {}),
        ("error", "improt json", {}),
        ("error", "improt os", {}),
    ]),
    ("Structural misconception — mutable default argument", [
        ("completed", "def add_item(item, bucket=[]):\n    bucket.append(item)\n    return bucket", {}),
    ]),
    ("Valid Python, wrong placement — accumulator resets inside loop", [
        ("completed", "for price in prices:\n    total = 0\n    total += price\nprint(total)", {}),
    ]),
    ("Valid Python, unreachable line — code after return", [
        ("completed", "def label(value):\n    return str(value)\n    print('done')", {}),
    ]),
    ("Valid Python, wrong placement — list recreated inside loop", [
        ("completed", "for word in words:\n    matches = []\n    matches.append(word)\nprint(matches)", {}),
    ]),
    ("Valid Python, likely early exit — unconditional return inside loop", [
        ("completed", "def first_value(values):\n    for value in values:\n        return value", {}),
    ]),
    ("Ambiguous placement — ask whether output is per item or final only", [
        ("completed", "for city in cities:\n    result = city.upper()\nprint(result)", {}),
    ]),
    ("Runtime failure — local value read before assignment", [
        ("completed", "def total_price():\n    print(total)\n    total = 0\n    return total", {}),
    ]),
]


def run_step(mentor: Mentor, session: Session, behavior: str, code: str, kw: dict):
    if behavior == "completed":
        return mentor.on_completed_line(session, code)
    if behavior == "error":
        return mentor.on_error(session, code)
    if behavior == "stuck":
        return mentor.on_stuck(session, code, 10)
    if behavior == "explain":
        return mentor.explain(session, kw["target"], code)
    if behavior == "why":
        return mentor.why(session, code, kw.get("line", 1), kw.get("symbol"))
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="write a Markdown report to this path")
    args = ap.parse_args()

    config = Config.from_env()
    learner = LearnerModel(config.learner_db or None)
    learner.reset("eval")  # deterministic
    mentor = Mentor(LLMClient(config), learner)
    store = SessionStore()

    out: List[str] = []
    def emit(s: str = "") -> None:
        print(s)
        out.append(s)

    emit(f"# CodeTutor evaluation report")
    emit(f"\n- LLM mode: **{config.mode}**  •  model: `{config.model}`")
    emit(f"- Goal: {WEATHER}\n")

    session = store.create(WEATHER, learner_id="eval")
    bp = mentor.blueprint(session)
    emit("## Blueprint\n")
    for i, s in enumerate(bp.blueprint or [], 1):
        emit(f"{i}. {s}")
    emit()

    for title, steps in SCENARIOS:
        emit(f"## {title}\n")
        for behavior, code, kw in steps:
            last = [l for l in code.splitlines() if l.strip()]
            shown = last[-1] if last else code
            msg = run_step(mentor, session, behavior, code, kw)
            emit(f"**[{behavior}]** `{shown}`" + (f"  (target=`{kw.get('target')}`)" if kw.get("target") else "")
                 + (f"  (line {kw.get('line')})" if kw.get("line") else ""))
            if msg:
                emit(f"> {msg.text}")
                if msg.curiosity:
                    emit(f">\n> _curiosity:_ {msg.curiosity}")
            else:
                emit("> (no message)")
            emit()

    p = learner.profile("eval")
    emit("## Learner model after the run\n")
    emit(f"- **Mastered:** {', '.join(p.mastered) or '—'}")
    emit(f"- **Practicing:** {', '.join(p.practiced) or '—'}")
    emit(f"- **Struggling:** {', '.join(p.struggling) or '—'}")
    emit(f"- **Recurring misconceptions:** {', '.join(p.recurring_misconceptions) or '—'}")

    if args.out:
        with open(args.out, "w") as f:
            f.write("\n".join(out) + "\n")
        print(f"\n[report written to {args.out}]", file=sys.stderr)


if __name__ == "__main__":
    main()
