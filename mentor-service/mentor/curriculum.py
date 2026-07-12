"""The learning path — a structured Python curriculum, beginner to expert.

This turns "I don't know what to build" into a guided, adaptive sequence. Each LEVEL
targets a set of concepts (the same keys the analyzer detects) and offers concrete
project goals. Suggestions are driven by the learner model: we find the earliest level
the learner hasn't mastered and propose projects that exercise the concepts they're
still missing. Fully deterministic — no LLM needed — so it works offline.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Set

from .analyzer import CONCEPT_LABELS


@dataclass
class Level:
    key: str
    title: str
    concepts: List[str]          # concept keys this level is meant to build
    projects: List[str]          # goal strings the learner can pick from
    blurb: str                   # one line on why this level matters


# Ordered from foundations to expert. Concept keys must exist in CONCEPT_LABELS.
LEVELS: List[Level] = [
    Level(
        key="foundations",
        title="Foundations",
        concepts=["variables", "conditionals", "loops", "lists", "dicts"],
        projects=[
            "a number-guessing game in the terminal",
            "a to-do list you can add to and print",
            "a temperature converter (Celsius <-> Fahrenheit)",
            "a tally counter that summarizes a list of words",
        ],
        blurb="The core building blocks: values, decisions, repetition, and collections.",
    ),
    Level(
        key="functions",
        title="Functions & structure",
        concepts=["functions", "comprehensions", "exceptions"],
        projects=[
            "a tip calculator split into small functions",
            "a word-frequency counter for a block of text",
            "a unit converter library with one function per conversion",
        ],
        blurb="Package logic into reusable functions and handle things going wrong.",
    ),
    Level(
        key="outside_world",
        title="Working with the outside world",
        concepts=["file_io", "json", "http"],
        projects=[
            "a weather app that fetches the temperature for a city",
            "a script that reads a CSV file and prints a summary",
            "a notes app that saves and loads notes from a JSON file",
        ],
        blurb="Real programs read files, call APIs, and persist data.",
    ),
    Level(
        key="program_design",
        title="Program design",
        concepts=["classes", "recursion"],
        projects=[
            "a BankAccount class with deposit and withdraw",
            "a recursive function that prints a folder tree",
            "a contact book built around a Contact class",
        ],
        blurb="Model your problem with your own types, and solve nested problems.",
    ),
    Level(
        key="real_world",
        title="Real-world Python",
        concepts=["async"],
        projects=[
            "an async scraper that fetches several URLs at once",
            "add unit tests to one of your earlier projects",
            "turn one of your scripts into a small command-line tool",
        ],
        blurb="Concurrency, testing, and shipping — how pros work day to day.",
    ),
]

# Teaching metadata is separate from progress counters so curricula can evolve without
# changing learner evidence. Every module states what counts as evidence, likely
# misconceptions, and questions that test the mental model rather than memorization.
CURRICULUM_DETAILS = {
    "foundations": {
        "prerequisites": [],
        "evidence": ["stores and changes values", "chooses behavior with a condition",
                     "repeats work", "uses collections"],
        "common_mistakes": ["using a name before assigning it",
                            "putting repeated work outside a loop",
                            "resetting a total or collection inside a loop"],
        "understanding_checks": ["Which lines run once and which repeat?",
                                 "What value does this name hold here?"],
    },
    "functions": {
        "prerequisites": ["variables", "conditionals", "loops"],
        "evidence": ["defines and calls a function", "uses parameters", "returns a result"],
        "common_mistakes": ["defining but not calling", "returning too early",
                            "using mutable defaults"],
        "understanding_checks": ["What enters this function?", "What result leaves it?"],
    },
    "outside_world": {
        "prerequisites": ["functions", "exceptions"],
        "evidence": ["handles external failure", "validates external data",
                     "reads files or calls an API"],
        "common_mistakes": ["assuming every request succeeds", "hard-coding secrets"],
        "understanding_checks": ["What can fail outside your program?",
                                 "How should invalid data be handled?"],
    },
    "program_design": {
        "prerequisites": ["functions", "dicts"],
        "evidence": ["groups related state and behavior", "explains why the design helps"],
        "common_mistakes": ["using a class when a function is enough", "sharing state accidentally"],
        "understanding_checks": ["What responsibility belongs here?",
                                 "What simpler design did you consider?"],
    },
    "real_world": {
        "prerequisites": ["functions", "exceptions", "file_io"],
        "evidence": ["writes a repeatable test", "packages a runnable program",
                     "explains configuration outside code"],
        "common_mistakes": ["testing implementation rather than behavior",
                            "blocking inside async code"],
        "understanding_checks": ["How can this be verified automatically?",
                                 "What must be configured outside the code?"],
    },
}


def module_details(level_key: str) -> dict:
    return CURRICULUM_DETAILS[level_key]


@dataclass
class LevelProgress:
    key: str
    title: str
    mastered: int
    total: int
    done: bool
    blurb: str


@dataclass
class Suggestion:
    goal: str
    rationale: str


def path_progress(mastered: Set[str]) -> List[LevelProgress]:
    """Progress per level given the set of mastered concept keys."""
    out: List[LevelProgress] = []
    for lvl in LEVELS:
        have = sum(1 for c in lvl.concepts if c in mastered)
        out.append(LevelProgress(
            key=lvl.key, title=lvl.title, mastered=have, total=len(lvl.concepts),
            done=(have == len(lvl.concepts)), blurb=lvl.blurb))
    return out


def current_level(mastered: Set[str]) -> Level:
    """The earliest level not yet fully mastered (or the last, if all done)."""
    for lvl in LEVELS:
        if any(c not in mastered for c in lvl.concepts):
            return lvl
    return LEVELS[-1]


def suggest_goals(mastered: Set[str], limit: int = 3) -> List[Suggestion]:
    """Suggest projects for where the learner is, favoring their missing concepts."""
    lvl = current_level(mastered)
    missing = [c for c in lvl.concepts if c not in mastered]
    missing_labels = ", ".join(CONCEPT_LABELS.get(c, c) for c in missing) or "review and reinforcement"

    rationale = (f"You're at the '{lvl.title}' stage. This builds "
                 f"{missing_labels}, which you haven't mastered yet.")
    suggestions = [Suggestion(goal=p, rationale=rationale) for p in lvl.projects[:limit]]

    # If this level is fully mastered, also offer a stretch project from the next level.
    idx = LEVELS.index(lvl)
    level_done = all(c in mastered for c in lvl.concepts)
    if level_done and idx + 1 < len(LEVELS):
        nxt = LEVELS[idx + 1]
        suggestions.append(Suggestion(
            goal=nxt.projects[0],
            rationale=f"Stretch goal — a first taste of '{nxt.title}'."))
    return suggestions[:limit]
