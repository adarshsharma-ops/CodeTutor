"""Regressions from a complete learner chat, including invalid input and pasted code."""
import ast

from mentor.analyzer import analyze
from mentor.lesson_progress import evaluate_project
from mentor.program_evidence import converter_evidence, conversion_direction


SCRIPT = '''choice = input("Convert (C)elsius to Fahrenheit or (F)ahrenheit to Celsius? ")
temp = float(input("Enter temperature: "))
choice = choice.lower()
if choice == "f":
    result = (temp - 32) * 5 / 9
elif choice == "c":
    result = temp * 9 / 5 + 32
else:
    print("Please enter C or F")
print(result)
'''


def test_transcript_invalid_input_is_detected_before_next_step():
    issues = analyze(SCRIPT).context_issues
    assert issues[0].code == "conditionally_unassigned"
    assert issues[0].line == 10
    assert "result" in issues[0].explanation


def test_script_does_not_require_an_unannounced_function():
    safe = SCRIPT.replace('    print("Please enter C or F")', '    raise ValueError("Please enter C or F")')
    result = evaluate_project("temperature converter", safe, run_passed=True)
    assert result["passed"]
    assert not any(c["id"] == "function" for c in result["checks"])


def test_transcript_invalid_input_prevents_completion():
    result = evaluate_project("temperature converter", SCRIPT, run_passed=True)
    assert not result["passed"]
    assert "assigned_paths" in {c["id"] for c in result["checks"] if not c["passed"]}


def test_a_random_function_and_magic_numbers_do_not_prove_conversion():
    code = 'def unrelated(x):\n    pass\nvalues = [9, 5, 32]\nprint(values)'
    result = evaluate_project("temperature conversion function", code, run_passed=True)
    failed = {c["id"] for c in result["checks"] if not c["passed"]}
    assert {"function", "formula"} <= failed


def test_equivalent_formula_and_renamed_parameter_are_recognized():
    code = 'def convert(reading):\n    return 32 + (1.8 * reading)\nprint(convert(float(input("C: "))))'
    assert evaluate_project("temperature conversion function", code, True)["passed"]
    assert conversion_direction(ast.parse('32 + reading * 1.8', mode='eval').body) == "c_to_f"


def test_incorrect_formula_containing_expected_numbers_is_rejected():
    code = 'def convert(reading):\n    return reading + 9 + 5 + 32\nprint(convert(0))'
    assert not converter_evidence(ast.parse(code))["formulas"]


def test_wrong_direction_is_only_reported_against_explicit_prompt():
    wrong = SCRIPT.replace('result = (temp - 32) * 5 / 9', 'result = temp * 9 / 5 + 32')
    assert any(i.code == "conversion_direction_mismatch" for i in analyze(wrong).context_issues)
    assert not any(i.code == "conversion_direction_mismatch" for i in analyze(SCRIPT).context_issues)


def test_definite_assignment_accepts_all_branches_and_early_returns():
    code = 'def choose(flag):\n    if flag:\n        value = 1\n    else:\n        return 0\n    return value'
    assert not any(i.code == "conditionally_unassigned" for i in analyze(code).context_issues)


def test_definite_assignment_accepts_prior_initialization():
    code = 'result = None\nif ready:\n    result = 3\nprint(result)'
    assert not any(i.code == "conditionally_unassigned" for i in analyze(code).context_issues)
