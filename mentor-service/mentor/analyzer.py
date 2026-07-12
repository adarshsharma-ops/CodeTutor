"""Deterministic code analysis — the cheap, fast, reliable layer.

This layer does NOT call the LLM. It answers questions that a parser answers better
than any model:
  * Is there a syntax error / typo?  (compile the source)
  * Is the current line complete or unfinished?  (e.g. `for i in ` is unfinished)
  * Which library/function symbols appear?  (drives the curiosity meter)

The LLM is only used later to *explain* what this layer detects.
"""
from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


@dataclass
class SyntaxIssue:
    line: int
    offset: int
    message: str          # raw message from the Python parser
    text: str             # the offending source line


@dataclass
class ContextIssue:
    """A syntactically valid pattern that is very likely misplaced or misleading."""
    code: str
    line: int
    related_line: int
    summary: str
    explanation: str
    ask_intent: bool = False


@dataclass
class Analysis:
    source: str
    syntax_issue: Optional[SyntaxIssue]
    imports: Set[str] = field(default_factory=set)          # e.g. {"requests", "json"}
    called_names: Set[str] = field(default_factory=set)     # e.g. {"get", "json"}
    concepts: Set[str] = field(default_factory=set)         # e.g. {"loops", "dicts", "recursion"}
    misconceptions: Set[str] = field(default_factory=set)   # e.g. {"mutable_default_arg"}
    context_issues: List[ContextIssue] = field(default_factory=list)
    # Per-concept structural fingerprints: distinct implementations, so repeated or
    # incrementally-typed snapshots of the SAME construct don't inflate mastery.
    fingerprints: dict = field(default_factory=dict)
    last_line: str = ""
    last_line_complete: bool = True   # False when the last non-empty line looks unfinished
    condition_hint: Optional[str] = None  # operator/intent guidance for a paused condition

    @property
    def has_syntax_issue(self) -> bool:
        return self.syntax_issue is not None


# Tokens that, if a line ends with them, signal the statement is unfinished.
_DANGLING_END = re.compile(
    r"(?:\b(?:in|and|or|not|if|else|elif|return|for|while|import|from|as|with|def|class|lambda)\b"
    r"|[=+\-*/%<>,([{:.]|==|!=|<=|>=|->)\s*$"
)


def analyze(source: str) -> Analysis:
    """Analyze a snapshot of source code."""
    issue = _find_syntax_issue(source)

    imports: Set[str] = set()
    called: Set[str] = set()

    # Only walk the AST if it parses cleanly; otherwise partial-parse best-effort.
    tree = _safe_parse(source)
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    called.add(func.attr)
                elif isinstance(func, ast.Name):
                    called.add(func.id)

    concepts: Set[str] = set()
    misconceptions: Set[str] = set()
    fingerprints: dict = {}
    context_issues: List[ContextIssue] = []
    if tree is not None:
        concepts = detect_concepts(tree, imports)
        misconceptions = detect_misconceptions(tree)
        fingerprints = concept_fingerprints(tree, imports)
        context_issues = detect_context_issues(tree)

    last_line = _last_nonempty_line(source)
    complete = _looks_complete(last_line)
    condition_hint = condition_guidance(last_line)

    return Analysis(
        source=source,
        syntax_issue=issue,
        imports=imports,
        called_names=called,
        concepts=concepts,
        misconceptions=misconceptions,
        context_issues=context_issues,
        fingerprints=fingerprints,
        last_line=last_line,
        last_line_complete=complete,
        condition_hint=condition_hint,
    )


def condition_guidance(line: str) -> Optional[str]:
    """Return beginner guidance when a paused if/elif/while condition is unfinished.

    This describes the decision the learner needs to make; it never invents the final
    operand. A colon means the condition is structurally complete and should stay quiet.
    """
    text = line.strip()
    match = re.match(r"^(if|elif|while)\s*(.*)$", text)
    if not match:
        return None
    expression = match.group(2).strip()
    if expression.endswith(":"):
        return None
    if not expression:
        return "Decide what yes-or-no question this block should ask before choosing an operator."
    if re.search(r"(?:==|!=|<=|>=|<|>)\s*$", expression):
        return ("The comparison operator is present, but Python still needs the value on its "
                "right. Ask what value forms the boundary or exact match.")
    if re.search(r"\b(and|or)\s*$", expression):
        word = re.search(r"\b(and|or)\s*$", expression).group(1)
        meaning = "both questions must be true" if word == "and" else "either question may be true"
        return f"`{word}` means {meaning}; add the second yes-or-no question it should combine."
    if re.search(r"\b(not|in|is)\s*$", expression):
        op = re.search(r"\b(not|in|is)\s*$", expression).group(1)
        meanings = {"not": "the condition should be reversed",
                    "in": "a value should belong to a collection",
                    "is": "two names should refer to the same object"}
        return f"`{op}` asks whether {meanings[op]}; complete the other side of that question."
    # A bare name/expression can be valid truthiness, but without a colon it may also be
    # a learner pausing before a comparison. Present both possibilities without warning.
    return ("This can be a truthiness check (for example, whether a value is non-empty or "
            "true). If you meant an exact value or boundary, choose a comparison first; "
            "otherwise finish the condition with a colon.")


def detect_context_issues(tree: ast.AST) -> List[ContextIssue]:
    """Detect a small set of high-confidence, valid-Python placement mistakes.

    These checks are intentionally conservative. Ambiguous intent belongs in an LLM
    question; deterministic corrections should be reserved for patterns whose behavior
    can be explained from Python's execution rules.
    """
    issues: List[ContextIssue] = []

    def check_block(body: List[ast.stmt]) -> None:
        terminated: ast.stmt | None = None
        for index, stmt in enumerate(body):
            if terminated is not None:
                issues.append(ContextIssue(
                    code="unreachable_statement",
                    line=getattr(stmt, "lineno", 1),
                    related_line=getattr(terminated, "lineno", 1),
                    summary="This line cannot run",
                    explanation=(
                        "Python leaves this block at the earlier return, raise, break, or "
                        "continue, so this later line is never reached."
                    ),
                ))
                break
            if isinstance(stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
                terminated = stmt

        for index, stmt in enumerate(body):
            nested_blocks: List[List[ast.stmt]] = []
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                                 ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
                nested_blocks.append(stmt.body)
            if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While, ast.If)):
                nested_blocks.append(stmt.orelse)
            if isinstance(stmt, ast.If):
                nested_blocks.append(stmt.body)
            if isinstance(stmt, ast.Try):
                nested_blocks.extend([stmt.body, stmt.orelse, stmt.finalbody])
                nested_blocks.extend(h.body for h in stmt.handlers)
            for nested in nested_blocks:
                check_block(nested)

            # Ambiguous but teachable: a loop computes a value and the next statement
            # prints it once. Ask whether the learner wanted one final value or one per
            # item instead of declaring the placement wrong.
            if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)) and index + 1 < len(body):
                assigned = {
                    n.id for inner in stmt.body for n in ast.walk(inner)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
                }
                nxt = body[index + 1]
                if isinstance(nxt, ast.Expr) and isinstance(nxt.value, ast.Call) \
                        and isinstance(nxt.value.func, ast.Name) and nxt.value.func.id == "print":
                    printed = {
                        n.id for arg in nxt.value.args for n in ast.walk(arg)
                        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                    }
                    shared = sorted(assigned & printed)
                    if shared:
                        name = shared[0]
                        issues.append(ContextIssue(
                            code="print_after_loop_intent",
                            line=getattr(nxt, "lineno", 1),
                            related_line=getattr(stmt, "lineno", 1),
                            summary="Print once or once per item?",
                            explanation=(
                                f"This `print` runs after the loop and therefore shows only "
                                f"the final `{name}` value. Putting it inside would print once "
                                "for every item."
                            ),
                            ask_intent=True,
                        ))

    def assigned_names(stmt: ast.stmt) -> Set[str]:
        if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            return set()
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        return {n.id for target in targets for n in ast.walk(target) if isinstance(n, ast.Name)}

    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            first_assignment: dict[str, int] = {}
            for stmt in node.body:
                for name in assigned_names(stmt):
                    first_assignment.setdefault(name, getattr(stmt, "lineno", 1))
                for inner in ast.walk(stmt):
                    if isinstance(inner, ast.AugAssign) and isinstance(inner.target, ast.Name):
                        name = inner.target.id
                        if name in first_assignment:
                            issues.append(ContextIssue(
                                code="accumulator_reset_inside_loop",
                                line=first_assignment[name],
                                related_line=getattr(inner, "lineno", first_assignment[name]),
                                summary=f"`{name}` resets on every loop",
                                explanation=(
                                    f"`{name}` is created again each time the loop repeats, so its "
                                    "earlier total is lost before it can grow."
                                ),
                            ))
                            break
                    if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) \
                            and isinstance(inner.func.value, ast.Name) \
                            and inner.func.attr in {"append", "extend", "add", "update"}:
                        name = inner.func.value.id
                        if name in first_assignment:
                            issues.append(ContextIssue(
                                code="collection_reset_inside_loop",
                                line=first_assignment[name],
                                related_line=getattr(inner, "lineno", first_assignment[name]),
                                summary=f"`{name}` starts empty on every loop",
                                explanation=(
                                    f"`{name}` is recreated each time the loop repeats, so items "
                                    "collected during the previous repeat are discarded."
                                ),
                            ))
                            break
            # An unconditional top-level return means this loop can never start its
            # second iteration. This is behaviorally certain; only the intention is open.
            for stmt in node.body:
                if isinstance(stmt, ast.Return):
                    issues.append(ContextIssue(
                        code="unconditional_return_inside_loop",
                        line=getattr(stmt, "lineno", 1),
                        related_line=getattr(node, "lineno", 1),
                        summary="This loop stops after its first item",
                        explanation=(
                            "`return` leaves the whole function immediately, so the loop "
                            "cannot continue to a second item."
                        ),
                    ))
                    break

    check_block(getattr(tree, "body", []))

    # Conservative local use-before-assignment check: only direct, sequential statements
    # in a function body. If a name is assigned anywhere in that function, Python treats
    # it as local; reading it in an earlier top-level statement raises UnboundLocalError.
    for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        params = {a.arg for a in (fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs)}
        if fn.args.vararg: params.add(fn.args.vararg.arg)
        if fn.args.kwarg: params.add(fn.args.kwarg.arg)
        assigned_later = {
            n.id for stmt in fn.body for n in ast.walk(stmt)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
        } - params
        seen_assigned = set(params)
        for stmt in fn.body:
            loads = {
                n.id for n in ast.walk(stmt)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            }
            for name in sorted((loads & assigned_later) - seen_assigned):
                issues.append(ContextIssue(
                    code="local_used_before_assignment",
                    line=getattr(stmt, "lineno", 1),
                    related_line=next((getattr(s, "lineno", 1) for s in fn.body
                                       if any(isinstance(n, ast.Name) and n.id == name
                                              and isinstance(n.ctx, ast.Store)
                                              for n in ast.walk(s))), getattr(stmt, "lineno", 1)),
                    summary=f"`{name}` is used before it has a value",
                    explanation=(
                        f"Because `{name}` is assigned later in this function, Python treats it "
                        "as a local name from the start and cannot read it before that assignment runs."
                    ),
                ))
            seen_assigned |= {
                n.id for n in ast.walk(stmt)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
            }
    # Stable ordering and no duplicate reports for the same rule/line.
    unique = {(i.code, i.line): i for i in issues}
    return sorted(unique.values(), key=lambda i: i.line)


# Human-readable labels for concept keys (used in prompts and the curiosity meter).
CONCEPT_LABELS = {
    "variables": "variables",
    "loops": "loops (for/while)",
    "conditionals": "if/else",
    "lists": "lists",
    "dicts": "dictionaries",
    "comprehensions": "comprehensions",
    "functions": "functions",
    "recursion": "recursion",
    "classes": "classes",
    "exceptions": "exception handling",
    "async": "async/await",
    "http": "HTTP requests",
    "json": "JSON parsing",
    "file_io": "file I/O",
    "dataframes": "tabular data (DataFrames)",
    "data_loading": "loading structured data",
    "data_cleaning": "cleaning missing or invalid data",
    "aggregation": "grouping and summarizing data",
    "visualization": "data visualization",
    "ml_splitting": "train/test data separation",
    "ml_preprocessing": "machine-learning preprocessing",
    "ml_training": "model training",
    "ml_prediction": "model prediction",
    "ml_evaluation": "model evaluation",
    "ml_cross_validation": "cross-validation",
    "ml_interpretation": "model interpretation",
}

MISCONCEPTION_LABELS = {
    # AST-detected misconceptions
    "mutable_default_arg": "mutable default argument (e.g. `def f(x=[])`)",
    "bare_except": "bare `except:` that swallows every error",
    # error-signature misconceptions (recurring syntax mistakes)
    "typo_import": "misspelling the `import` keyword",
    "missing_colon": "forgetting the `:` after if/for/while/def",
    "unterminated_string": "leaving a string quote unclosed",
    "unclosed_bracket": "leaving a bracket/paren unclosed",
    "assignment_in_condition": "using `=` (assignment) where `==` (comparison) was meant",
}


def detect_concepts(tree: ast.AST, imports: Set[str]) -> Set[str]:
    """Map an AST to the programming CONCEPTS it demonstrates. Deterministic — no LLM."""
    concepts: Set[str] = set()
    func_names: Set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            concepts.add("variables")
        elif isinstance(node, (ast.For, ast.While, ast.AsyncFor)):
            concepts.add("loops")
        elif isinstance(node, ast.If):
            concepts.add("conditionals")
        elif isinstance(node, ast.List):
            concepts.add("lists")
        elif isinstance(node, ast.Dict):
            concepts.add("dicts")
        elif isinstance(node, ast.Subscript):
            concepts.add("dicts")  # indexing/keys — close enough for exposure tracking
        elif isinstance(node, (ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp)):
            concepts.add("comprehensions")
        elif isinstance(node, ast.FunctionDef):
            concepts.add("functions")
            func_names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            concepts.add("classes")
        elif isinstance(node, (ast.Try, ast.Raise)):
            concepts.add("exceptions")
        elif isinstance(node, (ast.AsyncFunctionDef, ast.Await)):
            concepts.add("async")

    # Recursion: a function that calls itself by name.
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) \
                        and inner.func.id == node.name:
                    concepts.add("recursion")

    # Library-driven concepts.
    if imports & {"requests", "httpx", "urllib", "http"}:
        concepts.add("http")
    if "json" in imports:
        concepts.add("json")
    if imports & {"pandas", "polars"}:
        concepts.add("dataframes")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
            concepts.add("file_io")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "json":
            concepts.add("json")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr in {"read_csv", "read_json", "read_excel", "scan_csv"}:
                concepts.add("data_loading")
            if attr in {"dropna", "fillna", "drop_duplicates", "astype", "replace"}:
                concepts.add("data_cleaning")
            if attr in {"groupby", "agg", "pivot_table", "value_counts"}:
                concepts.add("aggregation")
            if attr in {"plot", "hist", "scatter", "bar"}:
                concepts.add("visualization")
            if attr in {"fit", "fit_transform"}:
                concepts.add("ml_training")
            if attr in {"predict", "predict_proba"}:
                concepts.add("ml_prediction")
            if attr in {"transform", "fit_transform"}:
                concepts.add("ml_preprocessing")
            if attr in {"score"}:
                concepts.add("ml_evaluation")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func.id
            if name == "train_test_split": concepts.add("ml_splitting")
            if name in {"cross_val_score", "cross_validate", "GridSearchCV", "RandomizedSearchCV"}:
                concepts.add("ml_cross_validation")
            if name in {"accuracy_score", "precision_score", "recall_score", "f1_score",
                        "mean_absolute_error", "mean_squared_error", "classification_report"}:
                concepts.add("ml_evaluation")
            if name in {"permutation_importance", "PartialDependenceDisplay"}:
                concepts.add("ml_interpretation")
        if isinstance(node, ast.With):
            concepts.add("file_io")  # commonly `with open(...)`

    return concepts


def concepts_from_line(line: str) -> Set[str]:
    """Best-effort concept detection from a SINGLE (possibly broken) line of source.

    Used for negative evidence: a syntax error usually means the code doesn't parse, so
    the AST is unavailable — but the offending line's leading keyword still tells us which
    concept the learner was attempting (e.g. `while r` -> loops).
    """
    s = line.strip()
    if not s:
        return set()
    first = s.split()[0].rstrip(":(")
    keyword_map = {
        "while": "loops", "for": "loops",
        "if": "conditionals", "elif": "conditionals", "else": "conditionals",
        "def": "functions", "class": "classes",
        "try": "exceptions", "except": "exceptions", "finally": "exceptions",
        "raise": "exceptions", "with": "file_io",
        "async": "async", "await": "async",
    }
    found: Set[str] = set()
    if first in keyword_map:
        found.add(keyword_map[first])
    if "await " in s or s.startswith("async"):
        found.add("async")
    if "open(" in s:
        found.add("file_io")
    return found


def _fp(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def concept_fingerprints(tree: ast.AST, imports: Set[str]) -> Dict[str, Set[str]]:
    """A stable, body-independent signature per concept instance.

    The goal: counting *distinct implementations*, not repeated snapshots. We fingerprint
    the defining HEADER of a construct (a loop's target/iter, a function's name+args, an
    if's test) — deliberately excluding the body — so that re-sending the same buffer, or
    typing a construct's body line by line, does NOT produce new fingerprints. Only a
    genuinely new/different construct does.
    """
    fps: Dict[str, Set[str]] = {}

    def add(concept: str, sig: str) -> None:
        fps.setdefault(concept, set()).add(_fp(f"{concept}|{sig}"))

    for node in ast.walk(tree):
        if isinstance(node, ast.For):
            add("loops", "for|" + ast.dump(node.target) + "|" + ast.dump(node.iter))
        elif isinstance(node, ast.AsyncFor):
            add("loops", "afor|" + ast.dump(node.target) + "|" + ast.dump(node.iter))
            add("async", "afor")
        elif isinstance(node, ast.While):
            add("loops", "while|" + ast.dump(node.test))
        elif isinstance(node, ast.If):
            add("conditionals", "if|" + ast.dump(node.test))
        elif isinstance(node, ast.FunctionDef):
            add("functions", "def|" + node.name + "|" + ast.dump(node.args))
        elif isinstance(node, ast.AsyncFunctionDef):
            add("functions", "adef|" + node.name + "|" + ast.dump(node.args))
            add("async", "adef|" + node.name)
        elif isinstance(node, ast.ClassDef):
            add("classes", "class|" + node.name)
        elif isinstance(node, ast.Try):
            add("exceptions", "try|" + ",".join(
                sorted(ast.dump(h.type) if h.type else "bare" for h in node.handlers)))
        elif isinstance(node, ast.Raise):
            add("exceptions", "raise|" + (ast.dump(node.exc) if node.exc else "reraise"))
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            add("comprehensions", "comp|" + ast.dump(node.elt))
        elif isinstance(node, ast.DictComp):
            add("comprehensions", "dcomp|" + ast.dump(node.key))
        elif isinstance(node, ast.Await):
            add("async", "await")
        elif isinstance(node, ast.Dict):
            add("dicts", "dict|" + str(len(node.keys)))   # coarse: reduces incremental churn
        elif isinstance(node, ast.List):
            add("lists", "list|" + str(len(node.elts)))

    # Recursion: a function that calls itself by name.
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) \
                        and inner.func.id == node.name:
                    add("recursion", "rec|" + node.name)

    # Library concepts: fingerprint by call sites so distinct uses count.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                if f.attr in {"get", "post", "put", "delete", "request"}:
                    add("http", "call|" + f.attr)
                if f.attr == "json":
                    add("json", "call|json")
            if isinstance(f, ast.Name) and f.id == "open":
                add("file_io", "open")
        if isinstance(node, ast.With):
            add("file_io", "with")
    if "json" in imports:
        add("json", "import")
    if imports & {"requests", "httpx", "urllib", "http"}:
        add("http", "import")
    if imports & {"pandas", "polars"}:
        add("dataframes", "import")
    data_calls = {
        "read_csv": "data_loading", "read_json": "data_loading", "read_excel": "data_loading",
        "scan_csv": "data_loading", "dropna": "data_cleaning", "fillna": "data_cleaning",
        "drop_duplicates": "data_cleaning", "astype": "data_cleaning", "replace": "data_cleaning",
        "groupby": "aggregation", "agg": "aggregation", "pivot_table": "aggregation",
        "value_counts": "aggregation", "plot": "visualization", "hist": "visualization",
        "scatter": "visualization", "bar": "visualization",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            concept = data_calls.get(node.func.attr)
            if concept:
                add(concept, "call|" + node.func.attr)
    ml_methods = {"fit": "ml_training", "fit_transform": "ml_training",
                  "predict": "ml_prediction", "predict_proba": "ml_prediction",
                  "transform": "ml_preprocessing", "score": "ml_evaluation"}
    ml_functions = {"train_test_split": "ml_splitting", "cross_val_score": "ml_cross_validation",
                    "cross_validate": "ml_cross_validation", "GridSearchCV": "ml_cross_validation",
                    "RandomizedSearchCV": "ml_cross_validation", "accuracy_score": "ml_evaluation",
                    "precision_score": "ml_evaluation", "recall_score": "ml_evaluation",
                    "f1_score": "ml_evaluation", "mean_absolute_error": "ml_evaluation",
                    "mean_squared_error": "ml_evaluation", "classification_report": "ml_evaluation",
                    "permutation_importance": "ml_interpretation"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            concept = ml_methods.get(node.func.attr)
            if concept: add(concept, "call|" + node.func.attr)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            concept = ml_functions.get(node.func.id)
            if concept: add(concept, "call|" + node.func.id)
    return fps


def detect_misconceptions(tree: ast.AST) -> Set[str]:
    """Deterministically catch a few classic recurring mistakes. No LLM."""
    found: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for default in node.args.defaults + node.args.kw_defaults:
                if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    found.add("mutable_default_arg")
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            found.add("bare_except")
    return found


def _safe_parse(source: str) -> Optional[ast.AST]:
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _find_syntax_issue(source: str) -> Optional[SyntaxIssue]:
    """Return the first syntax error, unless it is merely an *unfinished* last line.

    We deliberately swallow errors that are just 'the learner hasn't finished typing'
    so the stuck-nudge path can handle those instead of nagging about a SyntaxError.
    """
    try:
        compile(source, "<learner>", "exec")
        return None
    except SyntaxError as e:
        offending = (e.text or "").rstrip("\n")
        # If the error is on the last non-empty line AND that line looks unfinished,
        # treat it as "in progress", not a typo.
        last = _last_nonempty_line(source)
        if offending.strip() and offending.strip() == last.strip() and not _looks_complete(last):
            return None
        return SyntaxIssue(
            line=e.lineno or 0,
            offset=e.offset or 0,
            message=e.msg,
            text=offending,
        )


def _last_nonempty_line(source: str) -> str:
    for line in reversed(source.splitlines()):
        if line.strip():
            return line
    return ""


def _looks_complete(line: str) -> bool:
    """Heuristic: does this line look like a finished statement?

    Unfinished examples: `for i in `, `x = `, `data = response.`, `if `.
    Finished examples:  `import requests`, `x = 1`, `print(x)`.
    """
    stripped = line.rstrip()
    if not stripped:
        return True
    if _DANGLING_END.search(stripped):
        return False
    # Unbalanced brackets => still typing arguments/collection.
    if _unbalanced(stripped):
        return False
    return True


def _unbalanced(line: str) -> bool:
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: List[str] = []
    in_str: Optional[str] = None
    for ch in line:
        if in_str:
            if ch == in_str:
                in_str = None
            continue
        if ch in ("'", '"'):
            in_str = ch
        elif ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if not stack or stack[-1] != pairs[ch]:
                return True
            stack.pop()
    return bool(stack) or in_str is not None
