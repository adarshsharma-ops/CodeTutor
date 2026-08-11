"""Tests for the mentor orchestration in offline mode (deterministic, no network)."""
from mentor.config import Config
from mentor.llm import LLMClient
from mentor.learner_model import LearnerModel, MASTERY_CLEAN_USES
from mentor.mentor import Mentor, _parse_steps, _blueprint_quality_problems
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


def test_beginner_blueprint_quality_rejects_abstract_plan():
    steps = ["Clarify what the program does", "Choose a structure",
             "Represent the information", "Connect the pieces"]
    problems = _blueprint_quality_problems(steps, "beginner")
    assert any("exact python construct" in problem.lower() for problem in problems)
    assert any("abstract" in problem.lower() for problem in problems)


def test_cloud_beginner_blueprint_is_rewritten_when_too_abstract(tmp_path, monkeypatch):
    cfg = Config(
        openai_key="", openai_base_url="https://api.openai.com/v1",
        anthropic_key="test-key", anthropic_base_url="https://api.anthropic.com",
        model="claude-test", fast_model="claude-test", idle_seconds=10,
        request_timeout=30, learner_db=str(tmp_path / "m.db"), llm_mode="anthropic",
    )
    mentor = Mentor(LLMClient(cfg), LearnerModel(str(tmp_path / "m.db")))
    replies = iter([
        "1. Clarify what it does\n2. Choose a structure\n3. Represent data\n4. Connect pieces",
        "1. No import is needed because Python already has comparisons\n"
        "2. Define `ticket_price(age)` so the decision has one home\n"
        "3. Use `if` and `else` because only one price should be selected\n"
        "4. `return price`, then call it inside `print()` so the result is visible",
    ])
    calls = []
    def fake_chat(**_kwargs):
        calls.append(1)
        return next(replies), "claude-test"
    monkeypatch.setattr(mentor.llm, "chat_with_failover", fake_chat)
    session = Session(goal="an age-based ticket price", learner_level="beginner")
    message = mentor.blueprint(session)
    assert len(calls) == 2
    assert "ticket_price(age)" in " ".join(message.blueprint or [])
    assert message.via and "rewrite" in message.via


def test_level_specific_blueprint_quality_contracts():
    assert not _blueprint_quality_problems([
        "Choose the representation trade-off", "Validate external input",
        "Implement the focused behavior", "Test normal and error paths"], "intermediate")
    assert not _blueprint_quality_problems([
        "Define the architecture boundary", "Evaluate storage trade-offs",
        "Design failure and security behavior", "Add observability and scaling tests"], "advanced")


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
    assert "go through one item at a time" in msg.text.lower()


def test_stuck_mid_condition_teaches_operator_choice(tmp_path):
    m = _mentor(tmp_path)
    msg = m.on_stuck(_session(), "minimum_age = 18\nif age >=", idle_seconds=10)
    assert msg.kind == "stuck"
    assert "right" in msg.text.lower() and "value" in msg.text.lower()


def test_pause_after_import_suggests_goal_relevant_library_in_plain_language(tmp_path):
    m = _mentor(tmp_path)
    msg = m.on_stuck(_session(), "import", idle_seconds=10)

    assert msg.kind == "stuck"
    assert "requests" in msg.text.lower()
    assert "website or api" in msg.text.lower()
    assert "receiving a response" in msg.text.lower()


def test_pause_after_import_does_not_invent_library_for_vague_goal(tmp_path):
    m = _mentor(tmp_path)
    session = Session(goal="build my first Python program", learner_id="u")
    msg = m.on_stuck(session, "import", idle_seconds=10)

    assert "don't want to guess" in msg.text.lower()
    assert "what capability" in msg.text.lower()


def test_stalled_help_escalates_and_new_progress_resets_it(tmp_path):
    m = _mentor(tmp_path)
    session = Session(goal="build a small program", learner_id="u")
    first = m.on_stuck(session, "value = 1", 10).text
    second = m.on_stuck(session, "value = 1", 10).text
    fourth = m.on_stuck(session, "value = 1", 10).text
    fourth = m.on_stuck(session, "value = 1", 10).text
    reset = m.on_stuck(session, "value = 1\nprint(value)", 10).text

    assert first != second
    assert "def one_clear_action" in fourth
    assert "do this next" in reset.lower()


def test_explicit_fix_repairs_one_line_and_explains(tmp_path):
    m = _mentor(tmp_path)
    msg = m.fix_line(_session(), "improt requests\nprint('ready')", 1)

    assert msg.replacement == "import requests"
    assert "spelling" in msg.text.lower()
    assert msg.line == 1


def test_explicit_fix_preserves_indentation(tmp_path):
    m = _mentor(tmp_path)
    msg = m.fix_line(_session(), "if ready\n    print('yes')", 1)
    assert msg.replacement == "if ready:"


def test_beginner_todo_import_pause_says_no_library_is_needed(tmp_path):
    m = _mentor(tmp_path)
    session = Session(goal="build a to-do list", learner_id="u", learner_level="beginner")
    m.blueprint(session)
    msg = m.on_stuck(session, "import", 10)
    assert "does not need a library" in msg.text.lower()
    assert "tasks = []" in msg.text
    assert "sys" not in msg.text.lower()


def test_beginner_chat_directly_states_next_step(tmp_path):
    m = _mentor(tmp_path)
    session = Session(goal="build a to-do list", learner_id="u", learner_level="beginner")
    m.blueprint(session)
    msg = m.ask(session, "What should I do next?", "")
    assert "do this next" in msg.text.lower()
    assert "tasks = []" in msg.text.lower()


def test_todo_blueprints_change_by_teaching_level(tmp_path):
    m = _mentor(tmp_path)
    beginner = Session(goal="build a to-do list", learner_level="beginner")
    advanced = Session(goal="build a to-do list", learner_level="advanced")
    m.blueprint(beginner)
    m.blueprint(advanced)
    assert "tasks = []" in " ".join(beginner.blueprint)
    assert "domain boundary" in " ".join(advanced.blueprint).lower()


def test_beginner_temperature_blueprint_is_actionable(tmp_path):
    m = _mentor(tmp_path)
    session = Session(goal="a temperature converter Celsius <-> Fahrenheit",
                      learner_level="beginner")
    m.blueprint(session)
    joined = " ".join(session.blueprint).lower()
    assert "input()" in joined
    assert "no import is needed" in joined
    assert "float()" in joined
    assert "print()" in joined


def test_beginner_age_ticket_blueprint_gives_exact_sequence(tmp_path):
    m = _mentor(tmp_path)
    session = Session(goal="an age-based ticket price", learner_level="beginner")
    m.blueprint(session)
    joined = " ".join(session.blueprint).lower()
    assert "no import is needed" in joined
    assert "ticket_price(age)" in joined
    assert "if" in joined and "return" in joined
    assert "input()" in joined and "int()" in joined and "print()" in joined


def test_beginner_age_ticket_pause_completes_function_header(tmp_path):
    m = _mentor(tmp_path)
    session = Session(goal="an age-based ticket price", learner_level="beginner")
    m.blueprint(session)
    msg = m.on_stuck(session, "def ticketprice()", 10)
    assert "def ticket_price(age):" in msg.text
    assert "no import" in msg.text.lower()


def test_beginner_age_ticket_pause_after_branches_requests_return(tmp_path):
    m = _mentor(tmp_path)
    session = Session(goal="an age-based ticket price", learner_level="beginner")
    m.blueprint(session)
    code = """def ticketprice(age):
    if age >= 18:
        price = 1000
    else:
        price = 500"""
    msg = m.on_stuck(session, code, 10)
    assert "return price" in msg.text
    assert session.current_step == 2


def test_beginner_temperature_import_pause_gives_first_line(tmp_path):
    m = _mentor(tmp_path)
    session = Session(goal="a temperature converter Celsius <-> Fahrenheit",
                      learner_level="beginner")
    m.blueprint(session)
    msg = m.on_stuck(session, "import", 10)
    assert "does not need a library" in msg.text.lower()
    assert "temperature = input" in msg.text
    assert "what capability" not in msg.text.lower()


def test_empty_beginner_temperature_session_states_first_step(tmp_path):
    m = _mentor(tmp_path)
    session = Session(goal="a temperature converter Celsius <-> Fahrenheit",
                      learner_level="beginner")
    m.blueprint(session)
    msg = m.on_stuck(session, "", 10)
    assert "do this next" in msg.text.lower()
    assert "input()" in msg.text


def test_explicit_function_question_starts_guided_subgoal_without_full_solution(tmp_path):
    m = _mentor(tmp_path)
    session = Session(goal="temperature converter Celsius to Fahrenheit",
                      learner_level="beginner")
    msg = m.ask(session, "How do I create a function to convert Celsius to Fahrenheit?", "")
    assert session.guided_subgoal == "celsius_to_fahrenheit"
    assert "def double(number)" in msg.text
    assert "return (celsius * 9 / 5) + 32" not in msg.text


def test_stalled_requested_function_escalates_from_concept_to_formula(tmp_path):
    m = _mentor(tmp_path)
    session = Session(goal="temperature converter Celsius to Fahrenheit",
                      learner_level="beginner", guided_subgoal="celsius_to_fahrenheit")
    code = "def celsius_to_fahrenheit(celsius):"
    first = m.on_stuck(session, code, 10)
    second = m.on_stuck(session, code, 10)
    third = m.on_stuck(session, code, 10)
    assert "indented `return`" in first.text
    assert "multiply `celsius` by 9" in second.text
    assert "return (celsius * 9 / 5) + 32" in third.text


def test_completed_requested_function_clears_subgoal_and_teaches_return_use(tmp_path):
    m = _mentor(tmp_path)
    session = Session(goal="temperature converter Celsius to Fahrenheit",
                      learner_level="beginner", guided_subgoal="celsius_to_fahrenheit")
    code = "def celsius_to_fahrenheit(celsius):\n    return (celsius * 9 / 5) + 32"
    msg = m.on_stuck(session, code, 10)
    assert session.guided_subgoal == ""
    assert "store or print" in msg.text.lower()


def test_converter_does_not_advance_past_broken_upper_or_discarded_results(tmp_path):
    m = _mentor(tmp_path)
    session = Session(goal="temperature converter Celsius to Fahrenheit",
                      learner_level="beginner")
    m.blueprint(session)
    code = '''temperature = input("Temperature: ")
value = float(temperature)
unit = input("C or F: ")
def celsius_to_fahrenheit(celsius):
    return (celsius * 9 / 5) + 32
def fahrenheit_to_celsius(fahrenheit):
    return (fahrenheit - 32) * 5 / 9
if unit.upper == "C":
    celsius_to_fahrenheit(value)'''
    msg = m.on_completed_line(session, code)
    assert msg is not None
    assert "parentheses" in msg.text.lower()
    assert session.current_step == 2


def test_opened_block_is_treated_as_composition_not_immediate_error(tmp_path):
    m = _mentor(tmp_path)
    assert m.on_completed_line(_session(), "if ready:") is None


def test_unchanged_error_polling_does_not_become_recurring(tmp_path):
    m = _mentor(tmp_path)
    session = _session()
    messages = [m.on_error(session, "improt requests").text for _ in range(3)]
    assert not any("few times" in text.lower() for text in messages)


def test_converter_requires_return_value_to_be_used_before_testing_step(tmp_path):
    m = _mentor(tmp_path)
    session = Session(goal="temperature converter Celsius to Fahrenheit",
                      learner_level="beginner")
    m.blueprint(session)
    code = '''temperature = input("Temperature: ")
value = float(temperature)
unit = input("C or F: ")
def celsius_to_fahrenheit(celsius):
    return (celsius * 9 / 5) + 32
def fahrenheit_to_celsius(fahrenheit):
    return (fahrenheit - 32) * 5 / 9
if unit.upper() == "C":
    celsius_to_fahrenheit(value)'''
    msg = m.on_completed_line(session, code)
    assert msg is not None
    assert "returned value" in msg.text.lower()
    assert "do this next" in msg.text.lower()
    assert session.current_step == 4


def test_beginner_mastery_does_not_silence_current_project_guidance(tmp_path):
    m = _mentor(tmp_path)
    s = _session()
    # Master loops via DISTINCT implementations (loops-only lines, no other concept).
    for code in ("for i in range(5):\n    pass",
                 "for j in range(3):\n    pass",
                 "for k in range(9):\n    pass"):
        m.on_completed_line(s, code)
    assert "loops (for/while)" in m.learner.profile(s.learner_id).mastered
    # Historical mastery must not make Beginner mode abandon the active journey.
    msg = m.on_completed_line(s, "for n in range(2):\n    pass")
    assert msg is not None
    assert "stay out of the way" not in msg.text.lower()


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


def test_local_why_explains_selected_list_assignment_without_inventing_behavior(tmp_path):
    m = _mentor(tmp_path)
    session = Session(goal="build a to-do list", learner_id="u", learner_level="beginner")
    msg = m.why(session, "tasks = []\naddtask('Grocery')", 1)
    assert "creates a list" in msg.text
    assert "remember tasks" in msg.text
    assert "adds grocery" not in msg.text.lower()


def test_local_stuck_todo_never_uses_unrelated_function_template(tmp_path):
    m = _mentor(tmp_path)
    session = Session(goal="build a to-do list", learner_id="u", learner_level="beginner",
                      blueprint=["Create tasks", "Add one task", "Show tasks"])
    msg = m.on_stuck(session, "tasks = []", idle_seconds=10)
    assert "to-do-list" in msg.text.lower()
    assert "one_clear_action" not in msg.text


def test_what_is_wrong_uses_current_code_and_verified_semantics(tmp_path):
    m = _mentor(tmp_path)
    session = Session(goal="build a to-do list", learner_id="u", learner_level="beginner")
    code = "tasks = []\ndef addtask(task):\n    tasks.append(task)\naddtask.append(task)"
    msg = m.ask(session, "What's wrong in my current file?", code)
    assert "function, not a collection" in msg.text
    assert msg.via == "Python-aware diagnosis"


def test_what_is_wrong_never_calls_populated_file_empty(tmp_path):
    m = _mentor(tmp_path)
    session = Session(goal="build a to-do list", learner_id="u", learner_level="beginner",
                      blueprint=["Create tasks", "Add a task"])
    msg = m.ask(session, "What is wrong?", "tasks = []")
    assert "empty" not in msg.text.lower()
    assert "next unchecked" in msg.text.lower()


def test_fix_does_not_rewrite_valid_line_because_another_line_is_broken(tmp_path):
    m = _mentor(tmp_path)
    session = Session(goal="build a to-do list", learner_id="u", learner_level="beginner")
    code = "while True:\n    print(3. Exit)"
    msg = m.fix_line(session, code, 1)
    assert msg.replacement is None
    assert "line 2" in msg.text.lower()
    assert "will not rewrite" in msg.text.lower()


def test_support_ticket_none_answer_is_immediate_and_actionable(tmp_path):
    m = _mentor(tmp_path)
    session = Session(
        goal="a rule-based support-ticket classifier in plain Python",
        learner_id="u", learner_level="beginner",
    )
    code = '''
categories = {"billing": ["refund"]}
def classify(text):
    for category, keywords in categories.items():
        if any(word in text for word in keywords):
            return category
'''
    msg = m.ask(session, "It responds with None if no keywords match", code)
    assert msg.via == "verified project check"
    assert 'return "uncategorized"' in msg.text
    assert "not inside it" in msg.text


def test_implicit_none_answer_works_outside_one_lesson(tmp_path):
    m = _mentor(tmp_path)
    session = Session(goal="build a command router", learner_id="u",
                      learner_level="beginner")
    code = '''
def choose_route(command):
    if command == "start":
        return "launch"
print(choose_route("stop"))
'''
    msg = m.ask(session, "Why is the result showing None?", code)
    assert msg.via == "verified project check"
    assert "choose_route" in msg.text
    assert "explicit fallback" in msg.text
