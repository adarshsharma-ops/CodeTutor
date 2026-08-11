"""Tests for the deterministic analysis layer — no LLM involved."""
from mentor.analyzer import analyze, condition_guidance, detect_misconceptions, _looks_complete
import ast


def test_imports_and_calls_detected():
    a = analyze("import requests\nr = requests.get(url)")
    assert "requests" in a.imports
    assert "get" in a.called_names


def test_http_and_json_concepts():
    a = analyze("import requests\nr = requests.get(u)\ndata = r.json()")
    assert "http" in a.concepts
    assert "json" in a.concepts
    assert "variables" in a.concepts


def test_loop_and_dict_concepts():
    a = analyze("d = {'a': 1}\nfor k in d:\n    print(d[k])")
    assert "loops" in a.concepts
    assert "dicts" in a.concepts


def test_recursion_detected():
    a = analyze("def fac(n):\n    return 1 if n <= 1 else n * fac(n - 1)")
    assert "recursion" in a.concepts
    assert "functions" in a.concepts


def test_unfinished_line_is_not_an_error():
    a = analyze('cities = ["London"]\nfor i in ')
    assert a.syntax_issue is None          # unfinished, not a typo
    assert a.last_line_complete is False


def test_real_typo_is_flagged():
    a = analyze("improt requests")
    assert a.has_syntax_issue is True
    assert a.last_line_complete is True


def test_line_completeness_heuristics():
    assert _looks_complete("import requests") is True
    assert _looks_complete("x = 1") is True
    assert _looks_complete("for i in ") is False
    assert _looks_complete("total = ") is False
    assert _looks_complete("data = response.") is False
    assert _looks_complete("print(x") is False   # unbalanced paren


def test_mutable_default_arg_misconception():
    tree = ast.parse("def add(item, bucket=[]):\n    return bucket")
    assert "mutable_default_arg" in detect_misconceptions(tree)


def test_bare_except_misconception():
    tree = ast.parse("try:\n    x = 1\nexcept:\n    pass")
    assert "bare_except" in detect_misconceptions(tree)


def test_detects_accumulator_reset_inside_loop():
    code = "for price in prices:\n    total = 0\n    total += price\nprint(total)"
    issues = analyze(code).context_issues
    assert any(i.code == "accumulator_reset_inside_loop" and i.line == 2 for i in issues)


def test_detects_statement_after_return():
    code = "def label(value):\n    return str(value)\n    print('done')"
    issues = analyze(code).context_issues
    assert any(i.code == "unreachable_statement" and i.line == 3 for i in issues)


def test_does_not_flag_accumulator_created_before_loop():
    code = "total = 0\nfor price in prices:\n    total += price\nprint(total)"
    assert not any(not i.ask_intent for i in analyze(code).context_issues)


def test_detects_collection_recreated_inside_loop():
    code = "for word in words:\n    matches = []\n    matches.append(word)\nprint(matches)"
    assert any(i.code == "collection_reset_inside_loop" for i in analyze(code).context_issues)


def test_detects_unconditional_return_inside_loop():
    code = "def first_even(values):\n    for value in values:\n        return value\n"
    assert any(i.code == "unconditional_return_inside_loop" for i in analyze(code).context_issues)


def test_conditional_return_inside_loop_is_not_assumed_wrong():
    code = "def first_even(values):\n    for value in values:\n        if value % 2 == 0:\n            return value\n"
    assert not any(i.code == "unconditional_return_inside_loop" for i in analyze(code).context_issues)


def test_detects_local_use_before_assignment():
    code = "def total_price():\n    print(total)\n    total = 0\n    return total"
    assert any(i.code == "local_used_before_assignment" for i in analyze(code).context_issues)


def test_function_parameter_is_not_use_before_assignment():
    code = "def show(total):\n    print(total)"
    assert not any(i.code == "local_used_before_assignment" for i in analyze(code).context_issues)


def test_loop_target_is_not_use_before_assignment():
    code = "def show(tasks):\n    for task in tasks:\n        print(task)"
    assert not any(i.code == "local_used_before_assignment" for i in analyze(code).context_issues)


def test_detects_methods_accidentally_nested_in_init():
    code = """class TodoList:
    def __init__(self):
        self.tasks = []
        def add_task(self, task):
            self.tasks.append(task)
"""
    issue = next(i for i in analyze(code).context_issues
                 if i.code == "method_nested_inside_method")
    assert issue.line == 4 and issue.related_line == 2


def test_detects_direct_method_using_self_without_parameter():
    code = """class TodoList:
    def print_tasks():
        for task in self.tasks:
            print(task)
"""
    assert any(i.code == "method_missing_self" for i in analyze(code).context_issues)


def test_structural_facts_ground_loop_and_class_shape():
    code = """class TodoList:
    def show(self):
        for task in self.tasks:
            print(task)
"""
    facts = analyze(code).verified_facts
    assert any("direct methods: show" in fact for fact in facts)
    assert any("assigns: task" in fact for fact in facts)


def test_capitalized_function_asks_intent_instead_of_assuming_class():
    issues = analyze("def TodoList():\n    pass").context_issues
    issue = next(i for i in issues if i.code == "function_or_class_intent")
    assert issue.ask_intent is True
    assert "both can be valid" in issue.explanation.lower()


def test_print_after_loop_is_a_question_not_a_correction():
    code = "for city in cities:\n    result = city.upper()\nprint(result)"
    issue = next(i for i in analyze(code).context_issues if i.code == "print_after_loop_intent")
    assert issue.ask_intent is True


def test_detects_data_workflow_concepts():
    code = "import pandas as pd\ndf = pd.read_csv('demo.csv')\nsummary = df.dropna().groupby('kind').agg('count')\nsummary.plot()"
    concepts = analyze(code).concepts
    assert {"dataframes", "data_loading", "data_cleaning", "aggregation", "visualization"} <= concepts


def test_detects_ml_workflow_concepts():
    code = """from sklearn.model_selection import train_test_split, cross_val_score
from sklearn.metrics import f1_score
X_train, X_test, y_train, y_test = train_test_split(X, y)
model.fit(X_train, y_train)
predictions = model.predict(X_test)
score = f1_score(y_test, predictions)
folds = cross_val_score(model, X, y)
"""
    concepts = analyze(code).concepts
    assert {"ml_splitting", "ml_training", "ml_prediction", "ml_evaluation",
            "ml_cross_validation"} <= concepts


def test_condition_guidance_for_missing_comparison_value():
    hint = condition_guidance("if age >=")
    assert hint and "right" in hint.lower()


def test_condition_guidance_explains_and_or_semantics():
    assert "both" in condition_guidance("if adult and").lower()
    assert "either" in condition_guidance("if weekend or").lower()


def test_bare_condition_guidance_preserves_truthiness_option():
    hint = condition_guidance("if items")
    assert hint and "truthiness" in hint.lower() and "colon" in hint.lower()


def test_complete_conditions_do_not_trigger_guidance():
    assert condition_guidance("if active:") is None
    assert condition_guidance("if name in allowed_names:") is None
    assert condition_guidance("while running:") is None


def test_method_reference_compared_to_text_requires_call():
    result = analyze('if unit.upper == "C":\n    pass')
    issues = [i for i in result.context_issues if i.code == "method_reference_not_called"]
    assert len(issues) == 1
    assert "upper()" in issues[0].explanation


def test_returning_function_called_as_statement_discards_result():
    result = analyze("def convert(value):\n    return value * 2\nconvert(10)")
    issues = [i for i in result.context_issues if i.code == "discarded_return_value"]
    assert len(issues) == 1
    assert "store or print" in issues[0].explanation.lower()


def test_printing_returning_function_does_not_discard_result():
    result = analyze("def convert(value):\n    return value * 2\nprint(convert(10))")
    assert not any(i.code == "discarded_return_value" for i in result.context_issues)


def test_mutating_function_call_is_not_mislabeled_as_discarded_result():
    code = "tasks = []\ndef addtask(task):\n    tasks.append(task)\n    return tasks\naddtask('grocery')"
    result = analyze(code)
    assert not any(i.code == "discarded_return_value" for i in result.context_issues)


def test_menu_input_before_loop_is_detected():
    code = '''choice = input("Choice: ")
while True:
    if choice == "3":
        break'''
    issue = next(i for i in analyze(code).context_issues if i.code == "input_outside_loop")
    assert issue.line == 1
    assert "inside the loop" in issue.explanation


def test_input_refreshed_inside_loop_is_not_flagged():
    code = '''choice = input("Choice: ")
while True:
    choice = input("Choice: ")
    if choice == "3":
        break'''
    assert not any(i.code == "input_outside_loop" for i in analyze(code).context_issues)


def test_question_string_passed_as_task_is_detected():
    code = '''tasks = []
def addtask(task):
    tasks.append(task)
taskname = "What task would you like to add?"
addtask(taskname)'''
    issue = next(i for i in analyze(code).context_issues if i.code == "prompt_string_used_as_value")
    assert "input" in issue.explanation


def test_collection_method_on_function_is_detected():
    result = analyze("tasks = []\ndef addtask(task):\n    tasks.append(task)\naddtask.append(task)")
    issue = next(i for i in result.context_issues if i.code == "collection_method_on_function")
    assert issue.line == 4
    assert "function, not a collection" in issue.summary


def test_returning_function_object_is_explained():
    result = analyze("def addtask(task):\n    return addtask")
    assert any(i.code == "returns_function_object" for i in result.context_issues)


def test_menu_function_compared_without_call_is_detected():
    code = 'def showoptions():\n    return input("Choice: ")\nwhile showoptions != "3":\n    pass'
    result = analyze(code)
    assert any(i.code == "function_compared_without_call" for i in result.context_issues)


def test_repeated_input_helper_calls_in_conditions_are_detected():
    code = '''
def showoptions():
    return input("Choice: ")
while True:
    if showoptions() == "1":
        pass
    elif showoptions() == "2":
        pass
'''
    result = analyze(code)
    issue = next(i for i in result.context_issues
                 if i.code == "repeated_input_function_in_conditions")
    assert "store one result" in issue.explanation


def test_return_inside_function_does_not_make_top_level_code_unreachable():
    code = 'def showoptions():\n    return input("Choice: ")\nwhile True:\n    choice = showoptions()\n    break'
    result = analyze(code)
    assert not any(i.code == "unreachable_statement" for i in result.context_issues)
