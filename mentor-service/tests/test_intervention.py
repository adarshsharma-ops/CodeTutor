"""Deterministic learner-state and intervention-policy tests."""
from mentor.analyzer import analyze
from mentor.intervention import decide_intervention
from mentor.state import Session


def _decide(code: str, *, changed=False, idle=10, level=1,
            learner="beginner", event="stuck"):
    return decide_intervention(
        analyze(code), code_changed=changed, idle_seconds=idle,
        hint_level=level, learner_level=learner, event=event,
    )


def test_recent_completed_edit_is_progress_not_an_interruption():
    decision = _decide("total = 1", changed=True, idle=0, event="completed")
    assert decision.learner_state == "progressing"
    assert decision.should_intervene is False


def test_unfinished_statement_is_high_confidence_stuck():
    decision = _decide("for item in ")
    assert decision.learner_state == "stuck"
    assert decision.confidence >= 0.9
    assert "another_hint" in decision.actions


def test_started_import_is_library_selection_not_generic_syntax():
    decision = _decide("import")
    assert decision.learner_state == "stuck"
    assert "library" in decision.evidence[0].lower()


def test_parser_error_is_high_confidence_and_fixable():
    decision = _decide("improt requests")
    assert decision.learner_state == "error"
    assert decision.confidence == 0.99
    assert "fix_and_explain" in decision.actions


def test_valid_but_misplaced_code_is_off_path():
    code = "for price in prices:\n    total = 0\n    total += price"
    decision = _decide(code)
    assert decision.learner_state == "off_path"
    assert decision.confidence >= 0.95


def test_ambiguous_pause_checks_in_instead_of_guessing():
    decision = _decide("name = input('Name: ')", learner="advanced")
    assert decision.learner_state == "thinking"
    assert decision.confidence < 0.7


def test_repeated_unchanged_pause_becomes_confident_stuck():
    decision = _decide("name = input('Name: ')", level=3, changed=False)
    assert decision.learner_state == "stuck"
    assert decision.confidence >= 0.85


def test_beginner_empty_lesson_gets_proactive_start_help():
    decision = _decide("", learner="beginner")
    assert decision.learner_state == "stuck"
    assert decision.should_intervene is True


def test_session_records_progress_after_an_intervention():
    session = Session(goal="learn loops")
    session.observe_progress("for item in ")
    session.record_intervention("abc123", "for item in ")
    session.observe_progress("for item in items:\n    print(item)")
    assert session.intervention_outcomes["abc123"] == "progressed"


def test_explicit_feedback_can_mark_an_intervention_insufficient():
    session = Session(goal="learn loops")
    session.record_intervention("abc123", "for item in ")
    session.record_intervention_feedback("abc123", helpful=False)
    assert session.intervention_outcomes["abc123"] == "not_helpful"
