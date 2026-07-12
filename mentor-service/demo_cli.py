#!/usr/bin/env python3
"""CLI demo harness — feel the mentor AND the learner model, without the editor.

It replays a coding session line by line and shows what the mentor would say at each
trigger. Crucially, it also shows the LEARNER MODEL in action:
  * a concept gets mastered after repeated clean use -> the mentor backs off
  * the same typo repeated 3x -> flagged as a recurring misconception -> new strategy
  * the learner profile persists to local SQLite across runs

Run it (offline mode works with zero setup):
    python demo_cli.py            # evolve the model
    python demo_cli.py --reset    # wipe the demo learner first

Point it at a real LLM by setting OPENAI_API_KEY / OPENAI_BASE_URL / MENTOR_MODEL.
"""
from __future__ import annotations

import os
import sys
import textwrap

# Keep demo data local and separate from any real learner DB.
os.environ.setdefault("MENTOR_LEARNER_DB", os.path.join(os.path.dirname(__file__), "demo_learner.db"))

from mentor.config import Config
from mentor.llm import LLMClient
from mentor.learner_model import LearnerModel
from mentor.mentor import Mentor
from mentor.state import SessionStore

LEARNER = "demo-learner"
GOAL = "Build a small weather app that fetches the temperature for a city"

# (event_type, code_so_far). 'stuck' entries end on an unfinished line.
SCRIPT = [
    ("completed", "import requests"),
    ("completed", "import requests\nurl = \"https://api.example.com/weather?city=London\""),
    ("completed", "import requests\nurl = \"https://api.example.com/weather?city=London\"\nresponse = requests.get(url)"),
    ("completed", "import requests\nurl = \"https://api.example.com/weather?city=London\"\nresponse = requests.get(url)\ndata = response.json()"),
    # Practice loops several times so the concept becomes "mastered":
    ("completed", "cities = [\"London\", \"Paris\"]\nfor c in cities:\n    print(c)"),
    ("completed", "nums = [1, 2, 3]\nfor n in nums:\n    print(n * 2)"),
    ("completed", "for x in range(5):\n    print(x)"),
    ("completed", "for y in range(3):\n    print(y)"),
    # Now use a loop again — mentor should recognize mastery and back off:
    ("completed", "temps = [12, 15, 9]\nfor t in temps:\n    print(t)"),
    # Repeat the SAME typo three times -> recurring misconception -> different strategy:
    ("error", "improt requests"),
    ("error", "improt json"),
    ("error", "improt os"),
    # Stuck on an unfinished for-loop:
    ("stuck", "cities = [\"London\", \"Paris\", \"Tokyo\"]\nfor i in "),
]


def rule(char: str = "─", n: int = 72) -> str:
    return char * n


def show(kind: str, text: str, extra: str = "") -> None:
    label = {"blueprint": "🗺  BLUEPRINT", "next_step": "➡  NEXT STEP",
             "error": "⚠  ERROR COACH", "stuck": "💭  STUCK NUDGE"}.get(kind, kind.upper())
    print(f"\n{label}")
    for line in textwrap.wrap(text, width=72) or [""]:
        print(f"   {line}")
    if extra:
        print(f"   ↳ {extra}")


def print_profile(learner: LearnerModel) -> None:
    p = learner.profile(LEARNER)
    print("\n" + rule("═"))
    print("LEARNER MODEL (persisted to local SQLite)")
    print(rule("═"))
    print(f"  Mastered   : {', '.join(p.mastered) or '—'}")
    print(f"  Practicing : {', '.join(p.practiced) or '—'}")
    print(f"  Struggling : {', '.join(p.struggling) or '—'}")
    print(f"  Recurring misconceptions: {', '.join(p.recurring_misconceptions) or '—'}")


def main() -> None:
    config = Config.from_env()
    learner = LearnerModel(config.learner_db or None)
    if "--reset" in sys.argv:
        learner.reset(LEARNER)
        print("(demo learner reset)\n")

    mentor = Mentor(LLMClient(config), learner)
    store = SessionStore()

    print(rule("═"))
    print(f"CodeTutor demo  •  LLM mode: {config.mode}  •  learner: {LEARNER}")
    print(rule("═"))
    print(f"\nGoal: {GOAL}")

    session = store.create(GOAL, learner_id=LEARNER)
    bp = mentor.blueprint(session)
    show("blueprint", "Here's the plan we'll build toward:")
    for i, step in enumerate(bp.blueprint or [], 1):
        for j, line in enumerate(textwrap.wrap(step, width=66)):
            print((f"   {i:>2}. " if j == 0 else "       ") + line)

    for event_type, code in SCRIPT:
        print("\n" + rule())
        last = [ln for ln in code.splitlines() if ln.strip()][-1]
        print(f"Buffer ends: | {last}")
        if event_type == "completed":
            msg = mentor.on_completed_line(session, code)
        elif event_type == "error":
            msg = mentor.on_error(session, code)
        else:
            msg = mentor.on_stuck(session, code, config.idle_seconds)
        if msg:
            show(msg.kind, msg.text, extra=msg.curiosity or "")

    print_profile(learner)
    print(f"\nRun again to see the profile persist. Use --reset to start fresh.")


if __name__ == "__main__":
    main()
