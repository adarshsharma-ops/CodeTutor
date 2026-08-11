"""Regression tests derived from the July Ollama usability recording."""
from mentor.analyzer import analyze
from mentor.config import Config
from mentor.learner_model import LearnerModel
from mentor.llm import LLMClient
from mentor.mentor import Mentor
from mentor.reliability import beginnerize, response_is_grounded
from mentor.state import Session


def _local_mentor(tmp_path):
    cfg = Config(
        openai_key="", openai_base_url="http://127.0.0.1:11434/v1",
        anthropic_key="", anthropic_base_url="https://api.anthropic.com",
        model="qwen2.5-coder:7b", fast_model="qwen2.5-coder:7b",
        idle_seconds=10, request_timeout=30, learner_db=str(tmp_path / "m.db"),
        failover=False,
    )
    return Mentor(LLMClient(cfg), LearnerModel(str(tmp_path / "m.db")))


def _session():
    return Session(goal="build a to-do list", blueprint=[])


def test_local_blueprint_does_not_force_a_class(tmp_path):
    msg = _local_mentor(tmp_path).blueprint(_session())
    joined = " ".join(msg.blueprint or []).lower()
    assert "create `tasks = []`" in joined
    assert "define a class" not in joined


def test_local_auto_guidance_does_not_call_model_or_dump_solution(tmp_path, monkeypatch):
    mentor = _local_mentor(tmp_path)
    monkeypatch.setattr(mentor.llm, "chat_with_failover",
                        lambda **_: (_ for _ in ()).throw(AssertionError("model called")))
    msg = mentor.on_stuck(_session(), "def TodoList", 10)
    assert msg and "```" not in msg.text and "class TodoList" not in msg.text


def test_valid_loop_variable_claim_is_rejected():
    analysis = analyze("def show(self):\n    for task in self.tasks:\n        print(task)")
    hallucination = "The task variable is used before it has a value."
    assert not response_is_grounded(hallucination, analysis, automatic=False)


def test_automatic_finished_code_is_rejected_at_early_hint_level():
    analysis = analyze("class TodoList:\n    pass")
    answer = "```python\nclass TodoList:\n    def __init__(self):\n        self.tasks = []\n```"
    assert not response_is_grounded(answer, analysis, automatic=True, level=1)


def test_common_jargon_is_defined_in_plain_language():
    text = beginnerize("Complete the method signature and add a parameter.")
    assert "def" in text and "input name" in text
