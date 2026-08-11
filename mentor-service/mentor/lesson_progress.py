"""Durable lesson progress and deterministic checks for guided projects."""
from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class LessonProgress:
    learner_id: str
    goal: str
    learner_level: str
    blueprint: list[str]
    current_step: int = 0
    completed_steps: list[int] | None = None
    status: str = "in_progress"
    pathway_id: str = "python-foundations"
    pathway_version: str = "1.0.0"
    module_id: str = "values-and-variables"
    project_id: str = ""
    file_uri: str = ""
    code_fingerprint: str = ""
    checks: list[dict] | None = None
    last_activity: float = 0.0

    def to_dict(self) -> dict:
        value = asdict(self)
        value["completed_steps"] = self.completed_steps or []
        value["checks"] = self.checks or []
        return value


class LessonProgressStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS lesson_progress (
                    learner_id TEXT PRIMARY KEY, goal TEXT NOT NULL,
                    learner_level TEXT NOT NULL, blueprint_json TEXT NOT NULL,
                    current_step INTEGER NOT NULL DEFAULT 0,
                    completed_steps_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'in_progress',
                    pathway_id TEXT NOT NULL DEFAULT 'python-foundations',
                    pathway_version TEXT NOT NULL DEFAULT '1.0.0',
                    module_id TEXT NOT NULL DEFAULT 'values-and-variables',
                    project_id TEXT NOT NULL DEFAULT '', file_uri TEXT NOT NULL DEFAULT '',
                    code_fingerprint TEXT NOT NULL DEFAULT '', checks_json TEXT NOT NULL DEFAULT '[]',
                    last_activity REAL NOT NULL
                )""")

    def save(self, progress: LessonProgress) -> LessonProgress:
        progress.last_activity = time.time()
        with self._conn() as c:
            c.execute("""
                INSERT INTO lesson_progress VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(learner_id) DO UPDATE SET
                  goal=excluded.goal, learner_level=excluded.learner_level,
                  blueprint_json=excluded.blueprint_json, current_step=excluded.current_step,
                  completed_steps_json=excluded.completed_steps_json, status=excluded.status,
                  pathway_id=excluded.pathway_id, pathway_version=excluded.pathway_version,
                  module_id=excluded.module_id, project_id=excluded.project_id,
                  file_uri=excluded.file_uri, code_fingerprint=excluded.code_fingerprint,
                  checks_json=excluded.checks_json, last_activity=excluded.last_activity
            """, (progress.learner_id, progress.goal, progress.learner_level,
                  json.dumps(progress.blueprint), progress.current_step,
                  json.dumps(progress.completed_steps or []), progress.status,
                  progress.pathway_id, progress.pathway_version, progress.module_id,
                  progress.project_id, progress.file_uri, progress.code_fingerprint,
                  json.dumps(progress.checks or []), progress.last_activity))
        return progress

    def get(self, learner_id: str) -> LessonProgress | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM lesson_progress WHERE learner_id=?", (learner_id,)).fetchone()
        if not row:
            return None
        return LessonProgress(
            learner_id=row["learner_id"], goal=row["goal"], learner_level=row["learner_level"],
            blueprint=json.loads(row["blueprint_json"]), current_step=row["current_step"],
            completed_steps=json.loads(row["completed_steps_json"]), status=row["status"],
            pathway_id=row["pathway_id"], pathway_version=row["pathway_version"],
            module_id=row["module_id"], project_id=row["project_id"], file_uri=row["file_uri"],
            code_fingerprint=row["code_fingerprint"], checks=json.loads(row["checks_json"]),
            last_activity=row["last_activity"])

    def clear(self, learner_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM lesson_progress WHERE learner_id=?", (learner_id,))


def fingerprint(code: str) -> str:
    normalized = "\n".join(line.rstrip() for line in code.strip().splitlines())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16] if normalized else ""


def infer_project(goal: str) -> str:
    lowered = goal.lower()
    if "support-ticket classifier" in lowered or "support ticket classifier" in lowered:
        return "support-ticket-classifier"
    if "to-do" in lowered or "todo" in lowered or "task list" in lowered:
        return "todo-list"
    if "temperature" in lowered or "fahrenheit" in lowered or "celsius" in lowered:
        return "temperature-converter"
    return "custom-python-project"


def _block_guarantees_value(statements: list[ast.stmt]) -> bool:
    """Return True when normal execution through a block must return a value."""
    for statement in statements:
        if isinstance(statement, ast.Return):
            return statement.value is not None
        if isinstance(statement, ast.Raise):
            return True
        if isinstance(statement, ast.If):
            if (statement.orelse and _block_guarantees_value(statement.body)
                    and _block_guarantees_value(statement.orelse)):
                return True
        if isinstance(statement, ast.Try):
            paths = [statement.body, *(handler.body for handler in statement.handlers)]
            if paths and all(_block_guarantees_value(path) for path in paths):
                if not statement.finalbody or _block_guarantees_value(statement.finalbody):
                    return True
        if isinstance(statement, ast.Match):
            has_catch_all = any(
                isinstance(case.pattern, ast.MatchAs) and case.pattern.pattern is None
                for case in statement.cases
            )
            if (has_catch_all and statement.cases
                    and all(_block_guarantees_value(case.body) for case in statement.cases)):
                return True
    return False


def possible_implicit_none_functions(code: str, require_consumed: bool = True) -> list[str]:
    """Find locally defined functions whose consumed result can implicitly be ``None``.

    This is deliberately conservative: procedures called only for their side effects are not
    flagged, and a function must already return a value on at least one path before CodeTutor
    treats a missing return path as a likely learner error.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    functions = {
        node.name: node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    consumed: set[str] = set()
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        if not isinstance(call.func, ast.Name) or call.func.id not in functions:
            continue
        parent = parents.get(call)
        # ``do_work()`` as a standalone statement is normally a procedure. Everywhere else,
        # including print(do_work()), assignment, comparison and return, consumes its result.
        if not (isinstance(parent, ast.Expr) and parent.value is call):
            consumed.add(call.func.id)
    risky = []
    candidates = consumed if require_consumed else set(functions)
    for name in sorted(candidates):
        fn = functions[name]
        class ValueReturnFinder(ast.NodeVisitor):
            found = False

            def visit_Return(self, node: ast.Return) -> None:  # noqa: N802
                self.found = self.found or node.value is not None

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
                if node is fn:
                    self.generic_visit(node)

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
                return

        finder = ValueReturnFinder()
        finder.visit(fn)
        has_value_return = finder.found
        if has_value_return and not _block_guarantees_value(fn.body):
            risky.append(name)
    return risky


def evaluate_project(goal: str, code: str, run_passed: bool = False) -> dict:
    """Return evidence-based checks. The LLM never decides whether a lesson passed."""
    project = infer_project(goal)
    try:
        tree = ast.parse(code)
        syntax_ok = True
    except SyntaxError:
        tree = None
        syntax_ok = False
    checks: list[dict] = [{"id": "syntax", "label": "Program has valid Python syntax", "passed": syntax_ok}]
    text = code.lower()
    implicit_none = possible_implicit_none_functions(code) if syntax_ok else []
    if project == "support-ticket-classifier":
        functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)] if tree else []
        classify_fn = next((fn for fn in functions if "classif" in fn.name.lower()), None)
        input_used = bool(tree and any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "input"
            for n in ast.walk(tree)))
        lower_used = bool(tree and any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "lower"
            for n in ast.walk(tree)))
        category_dict = bool(tree and any(isinstance(n, ast.Dict) for n in ast.walk(tree)))
        loops = [n for n in ast.walk(classify_fn) if isinstance(n, (ast.For, ast.While))] if classify_fn else []
        match_test = bool(classify_fn and any(
            isinstance(n, (ast.Compare, ast.Call)) and
            ((isinstance(n, ast.Compare) and any(isinstance(op, ast.In) for op in n.ops)) or
             (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "any"))
            for n in ast.walk(classify_fn)))
        fallback_return = bool(classify_fn and classify_fn.name not in implicit_none)
        called = bool(tree and classify_fn and any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == classify_fn.name
            for n in ast.walk(tree)))
        displayed = bool(tree and any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print"
            for n in ast.walk(tree)))
        checks += [
            {"id": "input", "label": "Reads a support ticket", "passed": input_used},
            {"id": "normalize", "label": "Normalizes ticket text for reliable matching", "passed": lower_used},
            {"id": "categories", "label": "Stores categories and keywords as data", "passed": category_dict},
            {"id": "classifier", "label": "Defines and calls a classification function", "passed": bool(classify_fn and called)},
            {"id": "matching", "label": "Checks ticket text against category keywords", "passed": bool(loops and match_test)},
            {"id": "fallback", "label": "Returns a category when no keyword matches", "passed": fallback_return},
            {"id": "output", "label": "Displays the assigned category", "passed": displayed},
        ]
    elif project == "todo-list":
        calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)} if tree else set()
        loops = [n for n in ast.walk(tree) if isinstance(n, ast.While)] if tree else []
        loop_has_input = False
        for loop in loops:
            choice_names = {
                target.id for node in ast.walk(loop)
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name) and node.value.func.id == "input"
                for target in node.targets if isinstance(target, ast.Name)
            }
            compared_names = {
                node.id for comparison in ast.walk(loop) if isinstance(comparison, ast.Compare)
                for node in ast.walk(comparison) if isinstance(node, ast.Name)
            }
            if choice_names & compared_names:
                loop_has_input = True
                break
        printed_task_data = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print"
            and any(isinstance(arg, ast.Name) and "task" in arg.id.lower() for arg in n.args)
            for n in ast.walk(tree)
        ) if tree else False
        checks += [
            {"id": "collection", "label": "Stores tasks in a collection", "passed": bool(tree and any(isinstance(n, (ast.List, ast.Dict)) for n in ast.walk(tree)))},
            {"id": "add", "label": "Can add a task", "passed": ".append(" in text or "addtask" in calls or "add_task" in calls},
            {"id": "view", "label": "Can display saved tasks", "passed": printed_task_data},
            {"id": "menu", "label": "Repeats a menu until exit", "passed": bool(loops and "break" in text)},
            {"id": "fresh_choice", "label": "Reads a fresh menu choice inside the loop", "passed": loop_has_input},
            {"id": "invalid", "label": "Handles an invalid choice", "passed": "invalid" in text and "else" in text},
        ]
    elif project == "temperature-converter":
        checks += [
            {"id": "function", "label": "Defines a conversion function", "passed": bool(tree and any(isinstance(n, ast.FunctionDef) for n in ast.walk(tree)))},
            {"id": "formula", "label": "Uses a Celsius/Fahrenheit formula", "passed": ("9" in text and "5" in text and "32" in text)},
            {"id": "input", "label": "Accepts a value to convert", "passed": "input(" in text or bool(tree and any(isinstance(n, ast.FunctionDef) and n.args.args for n in ast.walk(tree)))},
            {"id": "output", "label": "Returns or displays the result", "passed": "return " in text or "print(" in text},
        ]
    else:
        checks += [{"id": "substance", "label": "Contains an executable Python structure", "passed": bool(tree and len(tree.body) >= 2)}]
    # The same return-path protection applies to every project. A lesson may expose a more
    # specific version of the check (the classifier's "fallback" above), so avoid duplicating it.
    if project != "support-ticket-classifier":
        checks.append({
            "id": "return_paths",
            "label": "Functions used as values return a result on every path",
            "passed": not implicit_none,
            "detail": ("Possible implicit None from: " + ", ".join(implicit_none))
                      if implicit_none else "",
        })
    checks.append({"id": "run", "label": "Learner confirmed a successful run", "passed": bool(run_passed)})
    passed = all(item["passed"] for item in checks)
    return {"project_id": project, "checks": checks, "passed": passed,
            "passed_count": sum(1 for item in checks if item["passed"]), "total": len(checks)}
