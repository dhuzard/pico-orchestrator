# Task-classifier benchmark

This benchmark isolates the only probabilistic boundary before deterministic
routing:

```
raw engineering request + repository evidence
                    |
                    v
              classifier
                    |
                    v
       schemas/task.schema.json
                    |
                    v
               route_task
```

It does **not** change production routing. The benchmark is model-independent:
Pi, Jev, Codex, Claude, a local model, or deterministic heuristics can all emit
the same prediction format and be scored against the same gold cases.

## Contract

Each case contains:

- `request`: the natural-language engineering request;
- `repository_evidence`: concise observable facts a classifier may use;
- `task_facts`: non-classifier fields produced by task decomposition
  (`id`, `goal`, `dependencies`, `acceptance_criteria`,
  `files_or_areas`);
- `gold_classification`: the bounded fields that the classifier must predict.

The classifier fields are derived from `schemas/task.schema.json` as all
required fields other than the five fixed task-fact fields. Today that is:

- `task_type`
- `failure_cost`
- `semantic_correctness`
- `ambiguity`
- `implementation_scope`
- `implementation_required`
- `cross_layer`
- `behavior_preservation`
- `diagnosis_required`
- `assumptions_must_be_explicit`
- `many_routine_decisions`
- `production_feature`
- `security_sensitive`
- `destructive_operation`

## Prediction format

Use JSON Lines, one object per case:

```json
{"case_id":"classifier-001","classification":{"task_type":"debugging","failure_cost":"high","semantic_correctness":"normal","ambiguity":5,"implementation_scope":"small","implementation_required":true,"cross_layer":false,"behavior_preservation":"required","diagnosis_required":true,"assumptions_must_be_explicit":true,"many_routine_decisions":false,"production_feature":false,"security_sensitive":false,"destructive_operation":false}}
```

Top-level metadata is permitted, but `classification` must contain exactly
the classifier fields, plus an optional unscored `escalation_evidence` map.
Predictions are validated by materializing the complete task and validating it
against `schemas/task.schema.json`. Like `route_task`, the evaluator rejects a
prediction (reported under `invalid_predictions`) whose escalating values
(`failure_cost=high`, `semantic_correctness=critical`, `ambiguity>=7`,
`behavior_preservation=required`, `assumptions_must_be_explicit`,
`security_sensitive`, `destructive_operation`) lack an `escalation_evidence`
entry with code `CLASSIFICATION_EVIDENCE_MISSING`. Gold classifications follow
the same rule and the rubric in the schema's field descriptions.

A case may also carry optional `notes` and an `expected_decision`
(`route`, `primary_intent`, `capacities: {step_id: tier}`); the evaluator fails
if the gold classification no longer produces that decision, so policy changes
that move a case are explicit.

## Run

```sh
python3 benchmarks/classification/evaluate.py \
  --predictions path/to/predictions.jsonl
```

For machine-readable output:

```sh
python3 benchmarks/classification/evaluate.py \
  --predictions path/to/predictions.jsonl \
  --json
```

Use `--require-all` for benchmark runs that must contain a prediction for
every case. Without it, partial files are accepted and reported with a coverage
fraction, which is useful during development.

## Metrics

The evaluator reports:

- **coverage** — fraction of gold cases with a valid prediction;
- **exact classification accuracy** — every classifier field is correct;
- **per-field accuracy** and **macro field accuracy**;
- **route accuracy** — candidate classification resolves to the same route as
  the gold classification through the repository's real routing engine;
- **policy under-routing rate** — candidate route is later than the gold route
  in the policy's explicit `precedence` order;
- **policy over-routing rate** — candidate route is earlier than the gold route;
- **execution-decision accuracy** — route, primary intent, and assigned capacity
  tiers all match the gold decision;
- **escalation rates** — for each escalating field, the fraction of scored
  predictions (and of their gold labels) using the escalating value.

"Under-routing" here is a policy-relative term, not an empirical safety claim.
It means the candidate classification produced a less-precedent route than the
gold classification under the current policy.

The evaluator never reimplements routing or capacity rules. It imports
`tools/routing_engine.py` and uses the repository's current policy, schema,
precedence, execution-plan materialization, and capacity assignment.

## Adding cases

Prefer cases that discriminate between policy decisions rather than trivial
paraphrases. Include:

- borderline ambiguity and failure-cost judgments;
- behavior-preservation/refactor cases;
- security/destructive operations;
- migrations;
- scientific/semantic correctness;
- cross-layer production features;
- routine low-risk implementation;
- analysis/review tasks with no implementation;
- operational work (`vcs_operation`, `issue_management`, `config_edit`,
  `ci_fix`) where repository-wide guardrails tempt over-classification.

Gold labels should be adjudicated deliberately. If humans disagree materially
on a field, record the case only after resolving the intended benchmark label;
future versions can add multi-rater uncertainty/calibration data separately.
