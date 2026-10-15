"""Conservative structural evidence. Never executes learner code."""
from __future__ import annotations

import ast
import math
import re


def linear_expression(node: ast.AST) -> tuple[str | None, float, float] | None:
    """Reduce arithmetic in one variable to (name, coefficient, offset)."""
    if isinstance(node, ast.Name):
        return node.id, 1.0, 0.0
    if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
        return None, 0.0, float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = linear_expression(node.operand)
        if value:
            sign = -1 if isinstance(node.op, ast.USub) else 1
            return value[0], sign * value[1], sign * value[2]
    if not isinstance(node, ast.BinOp):
        return None
    left, right = linear_expression(node.left), linear_expression(node.right)
    if left is None or right is None:
        return None
    ln, la, lb = left
    rn, ra, rb = right
    if ln and rn and ln != rn:
        return None
    name = ln or rn
    if isinstance(node.op, ast.Add):
        return name, la + ra, lb + rb
    if isinstance(node.op, ast.Sub):
        return name, la - ra, lb - rb
    if isinstance(node.op, ast.Mult) and not (la and ra):
        return name, la * rb + ra * lb, lb * rb
    if isinstance(node.op, ast.Div) and not ra and rb:
        return name, la / rb, lb / rb
    return None


def conversion_direction(node: ast.AST) -> str | None:
    value = linear_expression(node)
    if not value or not value[0]:
        return None
    _, slope, offset = value
    if math.isclose(slope, 1.8) and math.isclose(offset, 32):
        return "c_to_f"
    if math.isclose(slope, 5 / 9) and math.isclose(offset, -32 * 5 / 9):
        return "f_to_c"
    return None


def converter_evidence(tree: ast.AST | None) -> dict:
    nodes = list(ast.walk(tree)) if tree else []
    formulas = [n for n in nodes if isinstance(n, (ast.Assign, ast.Return))
                and conversion_direction(n.value)]
    functions = [fn for fn in nodes if isinstance(fn, ast.FunctionDef)
                 and any(isinstance(n, ast.Return) and n.value is not None
                         and conversion_direction(n.value) for n in ast.walk(fn))]
    directions = {conversion_direction(n.value) for n in formulas}
    return {"formulas": formulas, "functions": functions, "directions": directions}


def converter_mapping_issues(tree: ast.AST) -> list[tuple[int, str]]:
    """Check direction only when the input prompt explicitly defines each letter."""
    prompts = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        inputs = [n for n in ast.walk(node.value) if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Name) and n.func.id == "input"]
        for call in inputs:
            if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
                prompt = call.args[0].value.lower()
                explicit_source = (re.search(r"\(?c\)?elsius\s+to\s+fahrenheit", prompt)
                                   and re.search(r"\(?f\)?ahrenheit\s+to\s+celsius", prompt))
                if explicit_source:
                    for target in node.targets:
                        if isinstance(target, ast.Name): prompts[target.id] = {"c": "c_to_f", "f": "f_to_c"}
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        test = node.test
        if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
            continue
        if not isinstance(test.left, ast.Name) or test.left.id not in prompts:
            continue
        letter = test.comparators[0]
        if not isinstance(letter, ast.Constant) or not isinstance(letter.value, str):
            continue
        expected = prompts[test.left.id].get(letter.value.lower())
        for statement in node.body:
            for part in ast.walk(statement):
                if isinstance(part, (ast.Assign, ast.Return)) and part.value is not None:
                    actual = conversion_direction(part.value)
                    if expected and actual and actual != expected:
                        findings.append((part.lineno, f"The input prompt defines '{letter.value}' as "
                                         f"{'Celsius to Fahrenheit' if expected == 'c_to_f' else 'Fahrenheit to Celsius'}, "
                                         "but this branch applies the reverse formula. Align this branch with the prompt."))
    return findings


def conditional_assignment_issues(tree: ast.AST) -> list[tuple[int, str]]:
    """Find a read after a conditional where only some continuing paths assign it.

    Deliberately scoped to branch-created locals, avoiding unknown imports/globals.
    Paths ending in return/raise do not reach the later read.
    """
    findings: list[tuple[int, str]] = []

    def walk_block(statements, definite, conditional):
        definite, conditional = set(definite), set(conditional)
        for statement in statements:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = statement.args
                params = {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]}
                if args.vararg: params.add(args.vararg.arg)
                if args.kwarg: params.add(args.kwarg.arg)
                walk_block(statement.body, params, set())
                definite.add(statement.name)
                continue
            if isinstance(statement, ast.ClassDef):
                continue
            if isinstance(statement, ast.If):
                inspect(statement.test, definite, conditional)
                yes, yes_maybe, yes_end = walk_block(statement.body, definite, conditional)
                no, no_maybe, no_end = walk_block(statement.orelse, definite, conditional)
                paths = [(d, m) for d, m, end in [(yes, yes_maybe, yes_end), (no, no_maybe, no_end)] if not end]
                if not paths:
                    return definite, conditional, True
                guaranteed = set.intersection(*(d for d, _ in paths))
                possible = set.union(*(d | m for d, m in paths))
                definite, conditional = guaranteed, possible - guaranteed
                continue
            # Do not infer execution paths through loops/try/match yet.
            if isinstance(statement, (ast.For, ast.AsyncFor, ast.While, ast.Try, ast.Match)):
                continue
            inspect(statement, definite, conditional)
            if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                assigned = {n.id for target in targets for n in ast.walk(target)
                            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
                definite |= assigned
                conditional -= assigned
            if isinstance(statement, (ast.Return, ast.Raise)):
                return definite, conditional, True
        return definite, conditional, False

    def inspect(node, definite, conditional):
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                if child.id in conditional and child.id not in definite:
                    findings.append((child.lineno, child.id))

    walk_block(tree.body, set(), set())
    return sorted(set(findings))
