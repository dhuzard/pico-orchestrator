# Work modes for captain decomposition

`task_type` names the dominant kind of task. When a work mix affects
decomposition or delegation, `work_profile` records **how much
of each kind of work** the task requires, so the captain can decide whether to
keep it together or split it into dependent tasks before routing. It is a task
demand profile, not a model capability score.

| Mode | What it measures |
|---|---|
| `search` | Finding and checking relevant code, tests, interfaces, history, and decisions across the repository. |
| `analysis` | Diagnosing causes, reconciling evidence, defining contracts, and comparing design choices. |
| `writing` | Producing a specification, documentation, review explanation, or other prose artifact. |
| `implementation` | Changing code, configuration, or other executable artifacts. |
| `verification` | Designing and running tests, checking invariants, and independently reviewing results. |
| `coordination` | Sequencing dependent tasks, integrating work, and keeping several worker scopes consistent. |

Score each mode from **0 to 10**. Zero means the mode is absent. For a mode
that is present, use these shared anchors, then choose the nearest integer:

| Score | Demand anchor |
|---|---|
| 1–2 | One short, well-bounded action with an obvious result. |
| 3–4 | A local task with established patterns and a few checks. |
| 5–6 | Several related files or decisions; requires deliberate investigation or iteration. |
| 7–8 | Multiple layers or competing evidence; needs sustained reasoning and explicit verification. |
| 9–10 | Repository-wide or exceptional work with many coupled decisions and a long execution horizon. |

The score describes the **amount and difficulty** of that mode, not how
uncertain the intended behavior is. A clear but large migration can have low
ambiguity and high implementation, verification, and coordination scores. An
unclear one-line requirement can have high ambiguity and low implementation
demand. Failure cost and owner authority remain separate constraints.

## Captain decision loop

1. Inspect enough repository context to identify outputs and dependencies.
2. Classify the task's dominant `task_type` and ambiguity score. Add
   `ambiguity_assessment` for scores 4–10 and `work_profile` when the work mix
   affects decomposition. State the evidence behind unusually high scores.
3. Split a mode into its own task when it has a distinct deliverable, changes
   the contract for later work, needs independent review, or benefits from a
   fresh context. For example, search for conflicting API contracts can
   precede specification, which precedes implementation and verification.
4. Give each resulting task a bounded goal, acceptance criteria, dependencies,
   and write scope. Run `route_task` for **each** task. It selects the approved
   family, capacity tier, and execution plan; the captain schedules ready tasks
   and provides their context packages.
5. Check results and reclassify if discovery changes the task. Do not bypass
   required reviews or give workers a decision reserved for the user.

Example decomposition:

| Task | Search | Analysis | Writing | Implementation | Verification | Coordination |
|---|---:|---:|---:|---:|---:|---:|
| Locate the existing data contract | 7 | 4 | 2 | 0 | 2 | 1 |
| Resolve conflicting contract evidence | 4 | 8 | 5 | 0 | 3 | 2 |
| Implement the agreed contract | 2 | 4 | 0 | 7 | 5 | 3 |
| Independently verify the result | 3 | 6 | 3 | 0 | 8 | 1 |

These are rubric examples, not calibrated model thresholds. The current router
records `work_profile` but does not use its scores to select a model. The next
calibration step is to evaluate exact worker configurations by mode, role,
reasoning effort, and task horizon. Only measured capacity should turn these
scores into additional routing guards.
