# Delegation benchmark

This benchmark contract evaluates **execution topology**, not top-level model
routing. The active route remains determined by `route_task`; the experiment
asks whether a routed lead step benefits from nested delegation.

## Required arms

For representative tasks, compare at least:

1. `direct` — lead executes without nested workers;
2. `one_worker`;
3. `two_workers`;
4. `three_to_four_workers`;
5. `mixed_model_workers`;
6. `same_model_workers`.

Do not force an arm when the task cannot safely support it (for example,
parallel mutating workers without isolated worktrees). Record the exclusion
rather than weakening shared-state safeguards.

## Outcomes

Use `schemas/run-record.schema.json#/$defs/orchestration_metrics` and per-step
telemetry/provenance to compare:

- task correctness / acceptance-criteria completion;
- final task success;
- wall-clock duration;
- lead, worker, cached, and total tokens;
- estimated monetary cost;
- worker execution time and coordination time;
- conflicting edits;
- corrective turns and failed attempts;
- test and CI outcome;
- reviewer findings;
- human corrections required;
- reproducibility across repeated runs.

When explicit baselines exist, `tools/provenance.py` derives:

```text
coordination_tax_tokens = total_tokens - estimated_direct_tokens
coordination_tax_time   = coordination_ms
quality_gain            = delegated_quality - direct_baseline_quality
parallel_speedup        = estimated_serial_time_ms / wall_clock_ms
```

Quality scores are benchmark-defined values in `[0, 1]`; do not fabricate
them for ordinary runs.

## Experimental discipline

Keep the user objective, repository commit, acceptance criteria, project
constraints, validation commands, and task classification fixed across arms.
Record model/capacity changes explicitly rather than conflating them with
worker-count effects.

Prefer multiple runs per arm for stochastic systems. Compare distributions and
failure modes, not only the best run.

## Learning boundary

Results are observational. They must **not** tune the active run online.

Accumulated benchmark evidence can support a reviewed, versioned change to
`routing/delegation-policy.yaml` (for example a fan-out limit or a family
bias). The repository change and its rationale remain auditable.
