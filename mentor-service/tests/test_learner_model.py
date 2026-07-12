"""Tests for the persistent learner model — the project's core IP.

Uses pytest's tmp_path so each test gets its own local SQLite file.
"""
from mentor.learner_model import LearnerModel, MASTERY_CLEAN_USES, MISCONCEPTION_THRESHOLD


def _model(tmp_path):
    return LearnerModel(str(tmp_path / "learner.db"))


def test_concept_progresses_to_mastered(tmp_path):
    m = _model(tmp_path)
    for _ in range(MASTERY_CLEAN_USES):
        m.observe("u", {"loops"}, clean=True)
    profile = m.profile("u")
    assert "loops (for/while)" in profile.mastered
    assert profile.is_mastered("loops")


def test_concept_starts_as_practiced(tmp_path):
    m = _model(tmp_path)
    m.observe("u", {"dicts"}, clean=True)   # one clean use only
    profile = m.profile("u")
    assert "dictionaries" in profile.practiced
    assert "dictionaries" not in profile.mastered


def test_misconception_becomes_recurring(tmp_path):
    m = _model(tmp_path)
    for i in range(MISCONCEPTION_THRESHOLD):
        count = m.record_error_signature("u", "typo_import")
    assert count == MISCONCEPTION_THRESHOLD
    assert m.is_recurring("u", "typo_import") is True
    assert any("import" in s for s in m.profile("u").recurring_misconceptions)


def test_misconception_not_recurring_below_threshold(tmp_path):
    m = _model(tmp_path)
    m.record_error_signature("u", "missing_colon")
    assert m.is_recurring("u", "missing_colon") is False


def test_concept_errors_mark_struggling(tmp_path):
    m = _model(tmp_path)
    # Two mistakes involving loops, no clean mastery yet -> struggling.
    m.record_concept_errors("u", {"loops"})
    m.record_concept_errors("u", {"loops"})
    profile = m.profile("u")
    assert "loops (for/while)" in profile.struggling
    assert "loops (for/while)" not in profile.mastered


def test_clean_use_can_recover_from_struggling(tmp_path):
    m = _model(tmp_path)
    m.record_concept_errors("u", {"loops"})
    m.record_concept_errors("u", {"loops"})
    for _ in range(MASTERY_CLEAN_USES):
        m.observe("u", {"loops"}, clean=True)
    # Enough clean uses -> mastered takes over from struggling.
    assert "loops (for/while)" in m.profile("u").mastered


def test_persistence_across_instances(tmp_path):
    db = str(tmp_path / "shared.db")
    m1 = LearnerModel(db)
    for _ in range(MASTERY_CLEAN_USES):
        m1.observe("u", {"functions"}, clean=True)
    # A fresh instance pointed at the same file should see the mastery.
    m2 = LearnerModel(db)
    assert "functions" in m2.profile("u").mastered


def test_reset_clears_learner(tmp_path):
    m = _model(tmp_path)
    m.observe("u", {"loops"}, clean=True)
    m.reset("u")
    p = m.profile("u")
    assert not (p.mastered or p.practiced or p.struggling)


def test_reset_clears_structural_evidence(tmp_path):
    m = _model(tmp_path)
    fingerprints = {"loops": {"same-loop"}}
    m.observe("u", {"loops"}, clean=True, fingerprints=fingerprints)
    m.reset("u")
    # The same implementation must count again after a genuine reset.
    m.observe("u", {"loops"}, clean=True, fingerprints=fingerprints)
    assert "loops (for/while)" in m.profile("u").practiced


def test_learners_are_isolated(tmp_path):
    m = _model(tmp_path)
    for _ in range(MASTERY_CLEAN_USES):
        m.observe("alice", {"loops"}, clean=True)
    assert "loops (for/while)" in m.profile("alice").mastered
    assert m.profile("bob").mastered == []
