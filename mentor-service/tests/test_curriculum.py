"""Tests for the curriculum / learning-path suggestions."""
from mentor import curriculum
from mentor.curriculum import LEVELS
from mentor.analyzer import CONCEPT_LABELS


def test_all_curriculum_concepts_are_known():
    for lvl in LEVELS:
        for c in lvl.concepts:
            assert c in CONCEPT_LABELS, f"unknown concept key: {c}"


def test_new_learner_starts_at_foundations():
    lvl = curriculum.current_level(set())
    assert lvl.key == "foundations"


def test_suggestions_target_missing_concepts():
    sugg = curriculum.suggest_goals(set())
    assert sugg, "should suggest something for a new learner"
    # rationale should name a foundations concept the learner lacks
    assert any("loop" in s.rationale.lower() or "variabl" in s.rationale.lower()
               or "dict" in s.rationale.lower() for s in sugg)


def test_level_advances_as_concepts_are_mastered():
    foundations = {"variables", "conditionals", "loops", "lists", "dicts"}
    lvl = curriculum.current_level(foundations)
    assert lvl.key == "functions"   # moved past foundations


def test_path_progress_counts():
    mastered = {"variables", "loops"}
    prog = curriculum.path_progress(mastered)
    foundations = next(p for p in prog if p.key == "foundations")
    assert foundations.mastered == 2
    assert foundations.total == 5
    assert foundations.done is False


def test_fully_mastered_learner_lands_on_last_level():
    everything = {c for lvl in LEVELS for c in lvl.concepts}
    lvl = curriculum.current_level(everything)
    assert lvl.key == LEVELS[-1].key


def test_every_level_has_structured_teaching_metadata():
    for level in LEVELS:
        details = curriculum.module_details(level.key)
        assert details["evidence"]
        assert details["common_mistakes"]
        assert details["understanding_checks"]
