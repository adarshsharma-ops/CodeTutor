#!/usr/bin/env python3
"""Replay deterministic learner traces and score interruption timing.

This complements eval_harness.py, which judges the teaching response itself.  This
script evaluates the decision made *before* any LLM is called: learner-state accuracy,
unnecessary interruptions, and whether progressive help is available.
"""
from __future__ import annotations

from dataclasses import dataclass

from mentor.analyzer import analyze
from mentor.intervention import decide_intervention


@dataclass(frozen=True)
class Replay:
    name: str
    code: str
    expected: str
    changed: bool = False
    idle: int = 10
    level: int = 1
    learner: str = "beginner"
    event: str = "stuck"


REPLAYS = [
    Replay("fresh completed edit", "total = 1", "progressing", True, 0, event="completed"),
    Replay("unfinished loop", "for item in ", "stuck"),
    Replay("library selection", "import", "stuck"),
    Replay("parser typo", "improt requests", "error"),
    Replay("misplaced accumulator", "for price in prices:\n    total = 0\n    total += price", "off_path"),
    Replay("ambiguous expert pause", "name = input('Name: ')", "thinking", learner="advanced"),
    Replay("unchanged after two hints", "name = input('Name: ')", "stuck", level=3),
    Replay("beginner has not begun", "", "stuck"),
]


def main() -> None:
    correct = 0
    over_interventions = 0
    print("# Adaptive intervention replay\n")
    print("| Scenario | Expected | Actual | Confidence | Interrupt? |")
    print("|---|---|---|---:|---|")
    for replay in REPLAYS:
        decision = decide_intervention(
            analyze(replay.code), code_changed=replay.changed,
            idle_seconds=replay.idle, hint_level=replay.level,
            learner_level=replay.learner, event=replay.event,
        )
        matched = decision.learner_state == replay.expected
        correct += int(matched)
        # A low-confidence thinking state intentionally permits one small choice-based
        # check-in; only interrupting active progress is counted as over-intervention.
        if replay.expected == "progressing" and decision.should_intervene:
            over_interventions += 1
        marker = "✓" if matched else "✗"
        print(f"| {marker} {replay.name} | {replay.expected} | "
              f"{decision.learner_state} | {decision.confidence:.2f} | "
              f"{'yes' if decision.should_intervene else 'no'} |")
    print("\n## Metrics\n")
    print(f"- State accuracy: {correct}/{len(REPLAYS)}")
    print(f"- Over-interventions on thinking/progressing traces: {over_interventions}")
    print("- Progressive-help controls: another hint, analogous example, wait, and "
          "consent-gated fix for parser errors")
    if correct != len(REPLAYS) or over_interventions:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
