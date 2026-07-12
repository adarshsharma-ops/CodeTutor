# Contributing to CodeTutor

CodeTutor optimizes for learner understanding, not answer generation. A contribution is
successful when it helps a learner form a correct mental model and need less assistance
later.

## Before opening a change

- Keep the learner at the keyboard; do not generate complete project solutions.
- Prefer deterministic evidence for syntax and structural claims.
- Use an LLM to explain a verified finding or ask about ambiguous intent.
- Explain an idea in ordinary language before introducing terminology.
- Treat mistakes as evidence of thinking, never as a reason for shame.
- Label hypotheses and roadmap functionality honestly.

## Teaching feature requirements

Every new detector or curriculum behavior should include:

1. A learning objective and prerequisites.
2. The observable evidence used by the tutor.
3. At least one common misconception.
4. A beginner-readable what/where/why/how explanation.
5. A counterexample that must not be flagged.
6. Automated regression tests.
7. An evaluation-harness scenario for subjective teaching quality.

Ambiguous intent must produce a question, not a correction. Avoid claims about learning,
mastery, educational outcomes, security, or scale without evidence.

## Local checks

```bash
cd mentor-service
python -m pip install -r requirements.txt
pytest -q

cd ../extension
npm ci
npm run compile
```

Never commit `.env`, credentials, learner databases, real learner code, or proprietary
examples. Use synthetic examples in tests and documentation.
