from mentor.lesson_progress import (LessonProgress, LessonProgressStore,
                                    evaluate_project, fingerprint,
                                    possible_implicit_none_functions)


def test_progress_survives_a_new_store_instance(tmp_path):
    path = str(tmp_path / "learner.db")
    first = LessonProgressStore(path)
    first.save(LessonProgress(
        learner_id="local", goal="build a to-do list", learner_level="beginner",
        blueprint=["Create a list", "Add a task", "Show tasks"], current_step=2,
        completed_steps=[0, 1], file_uri="file:///lesson.py",
        code_fingerprint=fingerprint("tasks = []")))

    restored = LessonProgressStore(path).get("local")
    assert restored is not None
    assert restored.goal == "build a to-do list"
    assert restored.current_step == 2
    assert restored.completed_steps == [0, 1]
    assert restored.file_uri == "file:///lesson.py"


def test_todo_is_not_complete_without_successful_run_confirmation():
    code = '''
tasks = []
def addtask(task):
    tasks.append(task)
while True:
    print(tasks)
    choice = input("1 add, 2 view, 3 exit")
    if choice == "1":
        addtask(input("Task: "))
    elif choice == "3":
        break
    else:
        print("Invalid choice")
'''
    result = evaluate_project("build a to-do list", code, run_passed=False)
    assert result["passed"] is False
    assert result["passed_count"] == result["total"] - 1
    assert evaluate_project("build a to-do list", code, run_passed=True)["passed"] is True


def test_todo_does_not_pass_when_menu_or_invalid_handling_is_missing():
    code = "tasks = []\ntasks.append('one')\nprint(tasks)\n"
    result = evaluate_project("build a to-do list", code, run_passed=True)
    assert result["passed"] is False
    failed = {check["id"] for check in result["checks"] if not check["passed"]}
    assert {"menu", "invalid"} <= failed


def test_todo_is_not_ready_when_choice_is_read_only_before_loop():
    code = '''
tasks = []
def addtask(task):
    tasks.append(task)
choice = input("Choice: ")
while True:
    print(tasks)
    if choice == "1":
        addtask(input("Task: "))
    elif choice == "3":
        break
    else:
        print("Invalid choice")
'''
    result = evaluate_project("build a to-do list", code, run_passed=False)
    failed = {check["id"] for check in result["checks"] if not check["passed"]}
    assert "fresh_choice" in failed


def test_temperature_converter_has_deterministic_success_criteria():
    code = '''
def celsius_to_fahrenheit(celsius):
    return celsius * 9 / 5 + 32
print(celsius_to_fahrenheit(float(input("Celsius: "))))
'''
    result = evaluate_project("temperature converter", code, run_passed=True)
    assert result["passed"] is True


def test_syntax_error_never_completes_a_lesson():
    result = evaluate_project("temperature converter", "def convert(:", run_passed=True)
    assert result["passed"] is False
    assert result["checks"][0] == {
        "id": "syntax", "label": "Program has valid Python syntax", "passed": False}


def test_syntax_error_is_never_described_as_ready_to_run():
    result = evaluate_project("build a to-do list", 'tasks = []\nwhile True:\n    input("unfinished', run_passed=False)
    failed = {check["id"] for check in result["checks"] if not check["passed"]}
    assert "syntax" in failed and "run" in failed


def test_support_ticket_classifier_requires_an_explicit_fallback():
    code = '''
ticket = input("Ticket: ").lower()
categories = {"billing": ["refund"], "technical": ["error"]}
def classify(text):
    for category, keywords in categories.items():
        if any(word in text for word in keywords):
            return category
print(classify(ticket))
'''
    result = evaluate_project("a rule-based support-ticket classifier in plain Python", code, True)
    failed = {check["id"] for check in result["checks"] if not check["passed"]}
    assert failed == {"fallback"}
    complete = code.replace('print(classify(ticket))',
                            '    return "uncategorized"\nprint(classify(ticket))')
    assert evaluate_project("a rule-based support-ticket classifier in plain Python",
                            complete, True)["passed"] is True


def test_implicit_none_check_applies_to_custom_projects_too():
    code = '''
def choose_route(command):
    if command == "start":
        return "launch"
print(choose_route("stop"))
'''
    assert possible_implicit_none_functions(code) == ["choose_route"]
    result = evaluate_project("build a command router", code, run_passed=True)
    failed = {check["id"] for check in result["checks"] if not check["passed"]}
    assert failed == {"return_paths"}
    fixed = code.replace('        return "launch"',
                         '        return "launch"\n    return "unknown"')
    assert evaluate_project("build a command router", fixed, run_passed=True)["passed"] is True


def test_side_effect_procedure_does_not_require_a_return_value():
    code = '''
def greet(name):
    print(f"Hello {name}")
greet("Ada")
'''
    assert possible_implicit_none_functions(code) == []
