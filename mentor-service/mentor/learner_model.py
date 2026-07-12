"""The learner model — the real IP.

A persistent, per-learner model of what someone knows, is practicing, and struggles
with. Every mentor prompt is filtered through it: skip what they've mastered, and change
*strategy* (not volume) when a misconception recurs.

Storage is LOCAL SQLite so learner data stays on the machine and needs no infrastructure.
A shared deployment would need an authenticated external store, retention controls, and
an explicit learner-data policy; the LearnerModel API provides the abstraction boundary.

Concept lifecycle:
    unseen -> exposed -> practiced -> mastered
                  -> struggling (when related mistakes recur)

Evidence rules (deliberately simple, tune later):
    exposure_count : times a concept appeared in the buffer
    clean_uses     : times it appeared in code that PARSED with no syntax error
    mastered       : clean_uses >= MASTERY_CLEAN_USES and not currently struggling
    practiced      : clean_uses >= 1
    exposed        : seen but no clean use yet
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Dict, List, Set

from .analyzer import CONCEPT_LABELS, MISCONCEPTION_LABELS

MASTERY_CLEAN_USES = 3          # clean uses before we stop re-explaining a concept
MISCONCEPTION_THRESHOLD = 3     # repeats before we treat a mistake as a recurring misconception


def default_db_path() -> str:
    env = os.getenv("MENTOR_LEARNER_DB", "").strip()
    if env:
        return env
    # Local, per-user, off in a dotfolder.
    home = os.path.expanduser("~")
    d = os.path.join(home, ".codetutor")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "learner.db")


@dataclass
class LearnerProfile:
    mastered: List[str]
    practiced: List[str]
    struggling: List[str]
    recurring_misconceptions: List[str]

    def is_mastered(self, concept: str) -> bool:
        return CONCEPT_LABELS.get(concept, concept) in self.mastered

    def as_prompt_block(self) -> str:
        """Compact block injected into mentor prompts."""
        if not (self.mastered or self.practiced or self.struggling or self.recurring_misconceptions):
            return "Learner profile: brand new — no history yet. Explain generously."
        parts = []
        if self.mastered:
            # "Observed consistently" rather than "mastered": these are heuristic signals
            # of repeated clean use, not a validated claim that the learner has mastery.
            parts.append(f"Observed consistently (do NOT re-explain): {', '.join(self.mastered)}.")
        if self.practiced:
            parts.append(f"Practicing (a light touch is fine): {', '.join(self.practiced)}.")
        if self.struggling:
            parts.append(f"Struggling (explain patiently, try a different angle): {', '.join(self.struggling)}.")
        if self.recurring_misconceptions:
            parts.append(
                "RECURRING misconceptions — address the underlying idea, don't just correct syntax: "
                + "; ".join(self.recurring_misconceptions) + ".")
        return "Learner profile — " + " ".join(parts)


class LearnerModel:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or default_db_path()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS concepts (
                    learner_id TEXT, concept TEXT,
                    exposure_count INTEGER DEFAULT 0,
                    clean_uses INTEGER DEFAULT 0,
                    error_hits INTEGER DEFAULT 0,
                    last_seen REAL,
                    PRIMARY KEY (learner_id, concept)
                )""")
            c.execute("""
                CREATE TABLE IF NOT EXISTS misconceptions (
                    learner_id TEXT, signature TEXT,
                    count INTEGER DEFAULT 0,
                    last_seen REAL,
                    PRIMARY KEY (learner_id, signature)
                )""")
            # Distinct-implementation evidence: each (learner, concept, fingerprint) is
            # ONE independent demonstration. Prevents repeated snapshots inflating mastery.
            c.execute("""
                CREATE TABLE IF NOT EXISTS concept_evidence (
                    learner_id TEXT, concept TEXT, fingerprint TEXT,
                    first_seen REAL,
                    PRIMARY KEY (learner_id, concept, fingerprint)
                )""")

    # --- observation -----------------------------------------------------
    def observe(self, learner_id: str, concepts: Set[str], clean: bool,
                misconceptions: Set[str] | None = None,
                fingerprints: dict | None = None) -> None:
        """Record that these concepts appeared. `clean` = the buffer parsed OK.

        When `fingerprints` is provided (concept -> set of structural signatures),
        clean_uses only increments for NEW, previously-unseen fingerprints — so
        re-sending the same code doesn't count as a fresh demonstration. When it's
        omitted (e.g. direct unit-test calls), each clean call counts once, as before.
        """
        now = time.time()
        with self._conn() as c:
            for concept in concepts:
                new_clean = 0
                if clean:
                    if fingerprints is None:
                        new_clean = 1
                    else:
                        fps = fingerprints.get(concept) or {f"__{concept}"}
                        for fp in fps:
                            cur = c.execute(
                                "INSERT OR IGNORE INTO concept_evidence "
                                "(learner_id, concept, fingerprint, first_seen) VALUES (?,?,?,?)",
                                (learner_id, concept, fp, now))
                            if cur.rowcount:      # newly inserted -> distinct demonstration
                                new_clean += 1
                c.execute("""
                    INSERT INTO concepts (learner_id, concept, exposure_count, clean_uses, last_seen)
                    VALUES (?, ?, 1, ?, ?)
                    ON CONFLICT(learner_id, concept) DO UPDATE SET
                        exposure_count = exposure_count + 1,
                        clean_uses = clean_uses + ?,
                        last_seen = ?
                """, (learner_id, concept, new_clean, now, new_clean, now))
            for m in (misconceptions or set()):
                self._bump_misconception(c, learner_id, m, now)

    def record_concept_errors(self, learner_id: str, concepts: Set[str], weight: int = 1) -> None:
        """Negative evidence: the learner made a mistake involving these concepts.

        `weight` lets callers grade confidence — e.g. a recurring, clearly-conceptual
        mistake counts more than a one-off that might just be typing noise. Bumps
        error_hits (and exposure), which feeds the 'struggling' classification.
        Complements observe(), which records the positive/clean evidence.
        """
        if not concepts:
            return
        weight = max(1, int(weight))
        now = time.time()
        with self._conn() as c:
            for concept in concepts:
                c.execute("""
                    INSERT INTO concepts (learner_id, concept, exposure_count, error_hits, last_seen)
                    VALUES (?, ?, 1, ?, ?)
                    ON CONFLICT(learner_id, concept) DO UPDATE SET
                        exposure_count = exposure_count + 1,
                        error_hits = error_hits + ?,
                        last_seen = ?
                """, (learner_id, concept, weight, now, weight, now))

    def record_error_signature(self, learner_id: str, signature: str) -> int:
        """Record a normalized error signature; return its new recurrence count."""
        now = time.time()
        with self._conn() as c:
            self._bump_misconception(c, learner_id, signature, now)
            row = c.execute(
                "SELECT count FROM misconceptions WHERE learner_id=? AND signature=?",
                (learner_id, signature)).fetchone()
            return row["count"] if row else 1

    @staticmethod
    def _bump_misconception(c: sqlite3.Connection, learner_id: str, sig: str, now: float) -> None:
        c.execute("""
            INSERT INTO misconceptions (learner_id, signature, count, last_seen)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(learner_id, signature) DO UPDATE SET
                count = count + 1, last_seen = ?
        """, (learner_id, sig, now, now))

    # --- profiling -------------------------------------------------------
    def profile(self, learner_id: str) -> LearnerProfile:
        mastered, practiced, struggling = [], [], []
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM concepts WHERE learner_id=?", (learner_id,)).fetchall()
            for r in rows:
                label = CONCEPT_LABELS.get(r["concept"], r["concept"])
                if r["error_hits"] and r["error_hits"] >= 2 and r["clean_uses"] < MASTERY_CLEAN_USES:
                    struggling.append(label)
                elif r["clean_uses"] >= MASTERY_CLEAN_USES:
                    mastered.append(label)
                elif r["clean_uses"] >= 1:
                    practiced.append(label)

            mrows = c.execute(
                "SELECT signature, count FROM misconceptions WHERE learner_id=? AND count>=?",
                (learner_id, MISCONCEPTION_THRESHOLD)).fetchall()
            recurring = [MISCONCEPTION_LABELS.get(m["signature"], m["signature"]) for m in mrows]

        return LearnerProfile(sorted(mastered), sorted(practiced), sorted(struggling), recurring)

    def mastered_concepts(self, learner_id: str) -> set[str]:
        """Concept KEYS the learner has mastered (drives the curriculum)."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT concept FROM concepts WHERE learner_id=? AND clean_uses>=?",
                (learner_id, MASTERY_CLEAN_USES)).fetchall()
        return {r["concept"] for r in rows}

    def is_recurring(self, learner_id: str, signature: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT count FROM misconceptions WHERE learner_id=? AND signature=?",
                (learner_id, signature)).fetchone()
        return bool(row and row["count"] >= MISCONCEPTION_THRESHOLD)

    def reset(self, learner_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM concepts WHERE learner_id=?", (learner_id,))
            c.execute("DELETE FROM misconceptions WHERE learner_id=?", (learner_id,))
            c.execute("DELETE FROM concept_evidence WHERE learner_id=?", (learner_id,))
