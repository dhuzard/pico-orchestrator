# provenance/runs/

One JSON file per orchestration run, named `<run_id>.json`, validated
against `schemas/run-record.schema.json`. Created and appended to via
`tools/provenance.py` (`start_run`, `add_step`, `update_step`,
`update_orchestration_metrics`, `finish_run`)
-- never hand-edited. Routing and per-step telemetry, when observed, is stored
only in the provenance fields `routing_telemetry` and `steps[].telemetry`; it
must not be embedded in `route_task_result`. New runs may also carry
`orchestration_metrics`, a closed run-level summary for delegation fan-out,
tokens/cost, wall time, coordination tax, verification outcomes, and offline
benchmark analysis. Unobserved values remain explicit `null`.

This directory intentionally starts empty: no run record exists for the
v0.1-v0.3 historical work tracked in `backlog/backlog.yaml`, because there
is no reliable execution-provenance evidence for that work (it predates this
run-provenance mechanism). Backlog items may exist and be marked `completed`
without a corresponding run record; a run record is only ever created for
work actually executed and observed through this mechanism going forward.

See the "Backlog, run provenance, and GitHub synchronization" section of the
repository README for the full data model.
