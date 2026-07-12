"""Tests for the mentor orchestration in offline mode (deterministic, no network)."""
from mentor.config import Config
from mentor.llm import LLMClient
from mentor.learner_model import LearnerModel, MASTERY_CLEAN_USES
from mentor.mentor import Mentor, _parse_steps
from mentor.state import Session


def _mentor(tmp_path) -> Mentor:
    # Build an offline config directly so the test is hermetic and independent of
    # any .env file that may hold real keys.
    cfg = Config(
        openai_key="", openai_base_url="https://api.openai.com/v1",
        anthropic_key="", anthropic_base_url="https://api.anthropic.com",
        model="none", fast_model="none", idle_seconds=10, request_timeout=30,
        learner_db=str(tmp_path / "m.db"),
    )
    assert cfg.offline
    return Mentor(LLMClient(cfg), LearnerModel(str(tmp_path / "m.db")))


def _session(learner="u"):
    return Session(goal="build a weather app", learner_id=learner, blueprint=["step one"])


def test_blueprint_produces_steps(tmp_path):
    m = _mentor(tmp_path)
    s = _session()
    msg = m.blueprint(s)
    assert msg.kind == "blueprint"
    assert len(s.blueprint) >= 3


def test_blueprint_parser_caps_steps_and_drops_conversation():
    raw = "\n".join([f"{i}. Step {i}" for i in range(1, 11)] + ["11. Want to begin?"])
    steps = _parse_steps(raw)
    assert len(steps) == 8
    assert not any(step.endswith("?") for step in steps)


def test_completed_line_gives_next_step(tmp_path):
    m = _mentor(tmp_path)
    msg = m.on_completed_line(_session(), "import requests")
    assert msg is not None and msg.kind == "next_step"


def test_completed_hint_uses_edited_line_anchor(tmp_path):
    m = _mentor(tmp_path)
    msg = m.on_completed_line(_session(), "x = 1\ny = 2\nprint(x)", target_line=2)
    assert msg is not None and msg.line == 2


def test_error_path_detects_typo(tmp_path):
    m = _mentor(tmp_path)
    msg = m.on_error(_session(), "improt requests")
    assert msg.kind == "error"
    assert "import" in msg.text.lower()


def test_recurring_typo_changes_strategy(tmp_path):
    m = _mentor(tmp_path)
    s = _session()
    first = m.on_error(s, "improt requests").text
    m.on_error(s, "improt json")
    third = m.on_error(s, "improt os").text
    # After the threshold, the explanation strategy changes.
    assert first != third
    assert "differently" in third.lower() or "pattern" in third.lower()


def test_stuck_on_unfinished_loop(tmp_path):
    m = _mentor(tmp_path)
    msg = m.on_stuck(_session(), 'c = ["London"]\nfor i in ', idle_seconds=10)
    assert msg.kind == "stuck"
    assert "iterable" in msg.text.lower()


def test_stuck_mid_condition_teaches_operator_choice(tmp_path):
    m = _mentor(tmp_path)
    msg = m.on_stuck(_session(), "minimum_age = 18\nif age >=", idle_seconds=10)
    assert msg.kind == "stuck"
    assert "right" in msg.text.lower() and "value" in msg.text.lower()


def test_mastered_concept_makes_mentor_back_off(tmp_path):
    m = _mentor(tmp_path)
    s = _session()
    # Master loops via DISTINCT implementations (loops-only lines, no other concept).
    for code in ("for i in range(5):\n    pass",
                 "for j in range(3):\n    pass",
                 "for k in range(9):\n    pass"):
        m.on_completed_line(s, code)
    assert "loops (for/while)" in m.learner.profile(s.learner_id).mastered
    # A further loop-only line should now get the "back off" response.
    msg = m.on_completed_line(s, "for n in range(2):\n    pass")
    assert "stay out of the way" in msg.text.lower()


def test_explain_returns_concept_note(tmp_path):
    m = _mentor(tmp_path)
    msg = m.explain(_session(), "requests", "import requests")
    assert msg.kind == "explain"
    assert "http" in msg.text.lower()


def test_why_reports_line(tmp_path):
    m = _mentor(tmp_path)
    code = "import requests\nr = requests.get(u)"
    msg = m.why(_session(), code, line=1, symbol="requests")
    assert msg.kind == "why"
    assert msg.line == 1


def test_ask_returns_answer(tmp_path):
    m = _mentor(tmp_path)
    msg = m.ask(_session(), "what is a dictionary?", "d = {}")
    assert msg.kind == "answer"
    assert msg.text


def test_repeated_identical_snapshot_does_not_master(tmp_path):
    m = _mentor(tmp_path)
    s = _session()
    same = "nums = [1, 2, 3]\nfor n in nums:\n    print(n)"
    # Sending the SAME buffer many times must NOT reach mastery (one demonstration).
    for _ in range(6):
        m.on_completed_line(s, same)
    assert "loops (for/while)" not in m.learner.profile(s.learner_id).mastered


def test_distinct_loop_implementations_reach_mastery(tmp_path):
    m = _mentor(tmp_path)
    s = _session()
    # Three genuinely different loops -> three distinct demonstrations -> mastered.
    m.on_completed_line(s, "a = [1]\nfor x in a:\n    print(x)")
    m.on_completed_line(s, "b = 'hi'\nfor c in b:\n    print(c)")
    m.on_completed_line(s, "for i in range(5):\n    print(i)")
    assert "loops (for/while)" in m.learner.profile(s.learner_id).mastered


def test_repeated_loop_errors_make_loops_struggling(tmp_path):
    m = _mentor(tmp_path)
    s = _session()
    # Two broken 'while' lines -> loops error_hits -> struggling in the profile.
    m.on_error(s, 'r = ["a"]\nwhile r')
    m.on_error(s, 'x = 1\nwhile x')
    assert "loops (for/while)" in m.learner.profile(s.learner_id).struggling


def test_valid_but_misplaced_line_gets_context_correction(tmp_path):
    m = _mentor(tmp_path)
    code = "for price in prices:\n    total = 0\n    total += price\nprint(total)"
    msg = m.on_completed_line(_session(), code)
    assert msg is not None and msg.kind == "context_correction"
    assert msg.line == 2
    assert "every loop" in msg.text.lower()


def test_corrected_context_issue_prompts_retrieval_check(tmp_path):
    m = _mentor(tmp_path)
    s = _session()
    wrong = "for price in prices:\n    total = 0\n    total += price\nprint(total)"
    fixed = "total = 0\nfor price in prices:\n    total += price\nprint(total)"
    assert m.on_completed_line(s, wrong).kind == "context_correction"
    follow_up = m.on_completed_line(s, fixed)
    assert follow_up is not None and follow_up.kind == "understanding_check"
    assert "own words" in follow_up.text.lower()


def test_understanding_answer_is_handled_as_reflection(tmp_path):
    m = _mentor(tmp_path)
    s = _session()
    wrong = "for price in prices:\n    total = 0\n    total += price"
    fixed = "total = 0\nfor price in prices:\n    total += price"
    m.on_completed_line(s, wrong)
    m.on_completed_line(s, fixed)
    reply = m.ask(s, "It must start once so every loop adds to the same total", fixed)
    assert reply.kind == "understanding_check"


def test_ambiguous_print_placement_asks_intent(tmp_path):
    m = _mentor(tmp_path)
    code = "for city in cities:\n    result = city.upper()\nprint(result)"
    msg = m.on_completed_line(_session(), code)
    assert msg is not None and msg.kind == "context_question"
