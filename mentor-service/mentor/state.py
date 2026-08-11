"""Session state — the stateful lesson plan.

The blueprint is not a one-time message; it is a checklist the mentor tracks. Session
state also remembers which symbols the learner has already seen, which drives the
curiosity meter ("first time using `requests`?") and the fade-over-time behavior.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Set


@dataclass
class Session:
    goal: str
    learner_level: str = "beginner"
    learner_id: str = "local"          # persists the learner model across sessions
    pathway_id: str = "python-foundations"
    module_id: str = "values-and-variables"
    blueprint: List[str] = field(default_factory=list)
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    model_override: str = ""            # if set, use this model for ALL calls this session
    last_code: str = ""                # most recent buffer seen, so "ask" never answers blind
    pending_correction: str = ""       # context rule awaiting evidence of a learner fix
    awaiting_understanding: str = ""   # corrected rule awaiting learner explanation
    approach: str = ""                 # inferred/confirmed: functions, class, script, etc.
    hint_levels: Dict[str, int] = field(default_factory=dict)
    recent_advice: List[str] = field(default_factory=list)
    progress_code: str = ""             # snapshot used to distinguish progress from a stall
    current_step: int = 0
    guided_subgoal: str = ""             # explicit learner request currently being coached
    last_recorded_error_code: str = ""   # unchanged polling must not inflate recurrence

    # Curiosity meter / fade-over-time bookkeeping.
    seen_imports: Set[str] = field(default_factory=set)
    seen_calls: Set[str] = field(default_factory=set)
    hints_given: int = 0

    def next_hint_level(self, key: str) -> int:
        """Escalate help gradually for the same underlying obstacle."""
        level = min(self.hint_levels.get(key, 0) + 1, 4)
        self.hint_levels[key] = level
        return level

    def observe_progress(self, code: str) -> bool:
        """Reset escalation when the learner changes the program meaningfully."""
        normalized = "\n".join(line.rstrip() for line in code.strip().splitlines())
        changed = normalized != self.progress_code
        if changed:
            self.progress_code = normalized
            self.hint_levels.clear()
            self.recent_advice.clear()
            self.last_recorded_error_code = ""
        return changed

    def remember_advice(self, key: str) -> bool:
        """Return False when the same advice was emitted very recently."""
        if key in self.recent_advice[-4:]:
            return False
        self.recent_advice.append(key)
        self.recent_advice = self.recent_advice[-12:]
        return True

    def blueprint_text(self) -> str:
        if not self.blueprint:
            return "(no blueprint yet)"
        return "\n".join(f"{i+1}. {step}" for i, step in enumerate(self.blueprint))

    def current_step_text(self) -> str:
        if not self.blueprint:
            return "clarify the next useful outcome"
        index = min(self.current_step, len(self.blueprint) - 1)
        return self.blueprint[index]

    def new_symbols(self, imports: Set[str], calls: Set[str]) -> List[str]:
        """Return symbols appearing for the FIRST time, then mark them seen."""
        fresh: List[str] = []
        for name in sorted(imports - self.seen_imports):
            fresh.append(name)
        for name in sorted(calls - self.seen_calls):
            fresh.append(f"{name}()")
        self.seen_imports |= imports
        self.seen_calls |= calls
        return fresh

    @property
    def confidence_stage(self) -> str:
        """Simple fade model: the more the learner has done, the quieter we get."""
        if self.hints_given < 4:
            return "new"          # explain generously
        if self.hints_given < 12:
            return "warming"      # briefer
        return "confident"        # only high-value interventions


class SessionStore:
    """In-memory prototype session store.

    A shared deployment needs an authenticated, expiring external store so multiple
    replicas can share state without allowing sessions to grow without bound.
    """

    def __init__(self, max_sessions: int = 500) -> None:
        self._sessions: dict[str, Session] = {}
        self._max = max_sessions

    def create(self, goal: str, learner_id: str = "local",
               learner_level: str = "beginner", pathway_id: str = "python-foundations",
               module_id: str = "") -> Session:
        s = Session(goal=goal, learner_id=learner_id, learner_level=learner_level,
                    pathway_id=pathway_id, module_id=module_id)
        # Bound memory: evict the oldest sessions once the cap is reached (dicts keep
        # insertion order). Prevents an unbounded dictionary in a long-running process.
        while len(self._sessions) >= self._max:
            oldest = next(iter(self._sessions))
            del self._sessions[oldest]
        self._sessions[s.session_id] = s
        return s

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def restore(self, session: Session) -> Session:
        """Register a session reconstructed from durable local lesson progress."""
        while len(self._sessions) >= self._max:
            del self._sessions[next(iter(self._sessions))]
        self._sessions[session.session_id] = session
        return session
