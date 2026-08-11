# Tutor reliability

CodeTutor treats model output as an explanation candidate, not as ground truth.

## Reliability pipeline

1. Python's parser and AST establish syntax, class structure, method placement, loop
   bindings, and selected high-confidence misconceptions.
2. Ambiguous design choices become intent questions; CodeTutor does not silently choose
   a class, function, framework, or architecture.
3. Automatic help follows four progressive levels: guiding question, concept and
   location, smallest change in words, then a tiny analogous example.
4. Responses that contradict parser facts, reveal finished code too early, or make an
   unsupported variable diagnosis are replaced with verified guidance.
5. The extension discards responses produced for an editor version that the learner has
   already changed and suppresses duplicate UI messages.
6. Local-model tutoring is labeled experimental. Small local models are useful for
   private experimentation but must pass the same evaluation scenarios before being
   recommended to beginners.

Meaningful code changes reset the four-level sequence. If the code remains unchanged,
CodeTutor advances one level per configured idle interval and stops after the analogous
example until the learner edits or explicitly asks a question. Automatic help never
modifies the file.

The explicit **CodeTutor: Fix this line and explain** action is different: it previews a
single-line replacement and its explanation, then applies it only after confirmation.

## Teaching contracts

Beginner mode owns the sequence: it states the next observable action and why it is
needed instead of repeatedly asking the learner to invent the plan. Intermediate mode
recommends a direction and introduces choices. Advanced mode goes deeper into design,
architecture, quality, and trade-offs. The selected level is stored only in the active
session and may be changed at any time.

Goal-aware deterministic rules protect common beginner journeys from weak-model advice.
For example, an in-memory to-do list does not need `sys` or another import: pausing after
`import` directs the learner to remove it and create `tasks = []` instead.
For a temperature converter, the beginner blueprint states that no import is required,
starts with `input()`, explains numeric conversion with `float()`, and proceeds through
unit choice, formula, output, and basic known-value tests. An empty Beginner file receives
the first blueprint action after the configured idle interval.

When a Beginner explicitly asks how to build a specific function, CodeTutor creates a
guided subgoal. It first shows a small analogous function rather than the requested
solution. If the learner writes the requested function heading and remains stalled, the
inline help escalates from the role of an indented `return`, to the formula in words, to
one exact scoped return line. Completing the function clears the subgoal and teaches the
learner to store or display its returned value.

## Quiet intervention model

Automatic completed-line, syntax, semantic, and idle signals share one intervention
lifecycle. The extension waits through a short composition grace period, discards advice
for changed editor versions, and replaces one active coaching card instead of appending
every automatic observation to chat history. Explicit learner questions remain in the
conversation history.

Syntax and semantic facts take priority over generic idle guidance. Opening an indented
block is treated as normal composition, and repeated polling of unchanged incomplete code
does not count as a recurring misconception. Beginner guidance consistently uses **Do
this next / Why / Watch for** and is not silenced solely by historical mastery.

Temperature-converter progress now requires observable evidence: input collection,
numeric conversion, normalized unit selection, both correct formulas, and actual use of
the returned conversion value. The analyzer detects comparisons such as `.upper == "C"`
and calls whose returned values are discarded, preventing premature movement to testing.

## Running the quality gate

From `mentor-service`:

```bash
python3 eval_harness.py --out report.md
```

Run the same scenario set once per candidate model and compare the reports. The harness
includes regressions from a real to-do-list learning session: false loop-variable errors,
nested methods, missing `self`, architecture assumptions, solution leakage, and overly
long automatic answers.

The automated gate is necessary but not sufficient. A human reviewer should still score
correctness, beginner clarity, timing, productive struggle, and whether the learner can
explain the concept afterward.

## Lesson state is deterministic and durable

The active goal, blueprint position, completed steps, check evidence, teaching level,
file association, and last activity are stored in the local learner database. A service
restart therefore cannot send a learner back to the first blueprint step. On resume,
CodeTutor reconciles the saved step with the current source instead of trusting stale
model advice.

The **Run lesson check** action evaluates explicit project criteria. A lesson completes
only when its structural checks pass, the source parses, and the learner confirms a
successful behavioral run. Model prose is never treated as completion evidence. Passing
shows the next recommended lesson and preserves the learner's curriculum history.

Every mentor event also returns authoritative lesson progress. The extension updates the
blueprint immediately rather than attempting to infer progress from the wording of a
model response. When all structural requirements pass except the behavioral run, the
learner receives one quiet prompt to run the program and use **Run lesson check**.

Explicit diagnostic questions are parser-first. CodeTutor detects function/collection
confusion such as `add_task.append(...)`, returning a function object accidentally,
comparing a function without calling it, and repeatedly calling an input-producing menu
function in several conditions. Those verified explanations bypass the model. The most
recent Python document is retained when focus moves into mentor chat, preventing a visible
file from being described as empty.
