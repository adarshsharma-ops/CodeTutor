# CodeTutor curricula

Curricula are versioned learning contracts, not collections of generated answers. Each
manifest describes what a learner is trying to understand, prerequisite modules, evidence
the tutor may observe, likely misconceptions, retrieval questions, and suitable projects.

The current catalog is `python-foundations/v1/manifest.json`. Existing versions should be
treated as immutable after public release; create a new version for breaking changes.

Available pathways:

- `python-foundations/v1` — beginner Python mental models and projects.
- `python-for-data/v1` — tabular data, loading, cleaning, aggregation, joins, and communication.
- `ml-foundations/v1` — problem framing, leakage-safe evaluation, training, metrics, and limitations.

A module must use concept keys recognized by the deterministic analyzer. Adding a new
concept therefore requires analyzer support, evidence tests, counterexamples, and teaching
evaluation scenarios before curriculum content refers to it.

Future pathways such as Python for Data and ML/AI should live beside Python Foundations and
declare cross-path prerequisites explicitly. Do not mark a module complete based only on
reading content or receiving an LLM answer; completion evidence should require independent
application and an understanding check.
