# Autonomous execution reliability audit — 12 October 2026

Scope: `pi-coding-orchestrator` at `0ac32f0` plus the uncommitted changes on
`audit/autonomous-reliability-20261012`. This audit inspected the executable
extensions, Python adapters, launcher, state stores, policy, and tests. The
repository changes in this branch are bounded failure-path fixes; the gaps
below remain open.

## Decision

**NO-GO for unattended multi-step delivery.** The routing and isolation
boundaries work for bounded runs, but the repository has no durable dispatcher
that continues dependency-ready work after a process exits. Parent finalization
now rejects `COMPLETED` without each mandatory independent review, but review
→ repair loops and CI are still only specified in the prompt and policy. Since
21 October 2026 a run has a single-owner lease and a verified resume
checkpoint (A3, A10 mitigated), but the dispatcher gap (A1) remains.

## Confirmed findings

| ID | Severity | Reproduction and evidence | Consequence | Fix and regression |
| --- | --- | --- | --- | --- |
| A1 | High | `/deliver` creates mission state and sends one `/orchestrate` follow-up (`extensions/mission-controller.ts`); `tools/mission.py` explicitly performs no routing, worker, CI, or scheduling. Mission fields such as `max_agent_runs` and `max_repair_attempts_per_task` are stored but no executor consumes them. No event consumer dispatches the next dependency-ready item after that turn or after restart. | Silent stall, unbounded manual retry, and unfinished workflow. | Add a durable mission dispatcher with per-task leases, dependency-ready selection, bounded attempt budgets, and event-driven wakeups. Test a multi-task DAG across process restarts with a fake executor. Open. |
| A2 | High | The parent accepts `COMPLETED` for a `CLAUDE_CODEX` phase with only `claude_worker` recorded. Reproduced with a temporary repository and `parent_orchestration.start`, phase/worker events, then `finish`; output was `{'outcome': 'completed', 'workers': ['claude_worker']}`. `route_task` validates review topology, but the parent record does not persist or enforce the required execution plan. | Mandatory independent review and repair can be skipped; provenance may falsely indicate completion. | `finish(COMPLETED)` now rejects (`PARENT_REVIEW_MISSING`) any routed, non-blocked/failed phase lacking a completed worker for each `independent_review` step of its route's fixed topology in `routing/default-policy.yaml`; an unreadable policy fails closed (`PARENT_POLICY_UNAVAILABLE`). The lifecycle tool surfaces the code so the parent can record the review or finish `PARTIAL`. Regressions cover both review routes, a failed reviewer, a superseded blocked attempt, and the installed-copy integration path. Fixed in branch for the reviewer-presence gate; reviewer identity separation and bounded review → repair → re-verification remain open. |
| A3 | High | `extensions/orchestration-lifecycle.ts` restores an active run after checking its session marker and durable status. There is no cross-process claim or lease. Two Pi processes loading the same saved session can both restore and execute it. | Duplicate workers or GitHub mutation attempts, and conflicting provenance. | Add a single-owner run lease with expiry and recovery, plus idempotency keys at external mutation boundaries. Test simultaneous resume against one session. **Mitigated** (21 October 2026, revised after independent review of PR #40): `tools/run_lease.py` holds owner id, pid and pid start time, hostname plus machine key (boot id, machine id, pid namespace), heartbeat and expiry under a `flock` with atomic replace. `parent_orchestration.py start`/`resume` take it; every write (`event`, `finish`, `settle`, context checkpoints) requires `--owner` and is refused under the lease lock unless the caller holds the lease, or the lease is free and the caller is the recorded owner, so a process that slept past its expiry can neither close the new owner's run nor free its lease. A live lease refuses a second resume (`RUN_LEASE_HELD`), a stale one is reported (`RUN_LEASE_STALE`), and only an explicit `takeover`, recorded in `lease_takeovers`, recovers it; an expired holder still running on the same machine needs a separate force reason (`RUN_LEASE_HOLDER_ALIVE`), which no automatic path supplies. The same owner from another pid needs the explicit launcher-to-child hand-off. The lifecycle detaches when the lease is lost or not renewed within its lifetime minus a margin, aborting running ClaudeWorkers and the current agent operation. Regressions cover concurrent acquire from eight processes, concurrent resume from two processes, the sleep-then-wake takeover, a running holder, pid reuse, containers sharing a hostname, implausible timestamps, hand-off rules, owner-only release, self-expiry with in-flight workers, and a failed hand-off. Open: idempotency keys at GitHub and other external mutation boundaries; pid start time and machine key are Linux-only (elsewhere the hostname and a live pid decide); no real two-Pi resume or real sleep/wake was exercised. |
| A4 | Medium | Python parent events read and rewrite the same JSON record. Concurrent events could overwrite a prior phase. | Incomplete provenance. | Added bounded per-run advisory locking around parent event and finish writes. Regression injects delayed concurrent writes and verifies all phases remain. Fixed in branch. |
| A5 | Medium | Local Python adapters in `model-router.ts` and `orchestration-lifecycle.ts` previously had no deadline; the router also buffered stdout without a limit. A hung or flooding adapter could stop progress or exhaust memory. | Indefinite wait or process failure. | Added 60-second routing and 30-second parent deadlines, plus an 8 MiB routing protocol limit. Tests launch actual hanging and flooding child processes. Fixed in branch. |
| A6 | Medium | An aborted agent activity with a queued follow-up reached `agent_before_settle`; the handler previously settled the parent without checking `ctx.hasPendingMessages()`. | Premature parent termination. | Skip settlement while a continuation is queued. Regression covers both `event.continue` and pending messages. Fixed in branch. |
| A7 | Medium | A same-session marker pointed at the original target but `restore` silently ignored a different current worktree, allowing the next `/orchestrate` to create another run. | Orphaned active parent and duplicate run identity. | Block the continuation on worktree mismatch or malformed active marker. Regression checks no replacement run starts. Fixed in branch. |
| A8 | Medium | `finish(COMPLETED)` accepted a phase or worker still marked `in_progress`. | Incomplete provenance and premature success. | Reject completion while any recorded phase or worker is nonterminal. Earlier failed/blocked attempts may remain in a repaired run. Regression covers phase and worker cases. Fixed in branch. |
| A9 | Medium | `context_budget.checkpoint` chooses its sequence and writes a checkpoint before the parent event acquires the new per-run lock. Concurrent checkpoints can choose the same sequence and overwrite one artifact. | Checkpoint reference may name the wrong saved state. | Sequence selection, file publication, and the event link now run under the same per-run lock (the link uses the unlocked writer, since `flock` is not re-entrant per open file). Regression runs four slowed concurrent checkpoints and verifies sequences 0001–0004, matching artifacts, and links. Since #35 removed automatic compaction, only the optional `context_budget.py checkpoint` command reaches this path. Fixed in branch. |
| A10 | Medium | Resume verifies repository identity but not target revision or a journal of integrated changes. Worker approvals are reset, yet the checkout may have moved between process runs. | Unsafe continuation or work repeated against a different revision. | Persist a verified checkout checkpoint and require reconciliation before mutation after restart. Test revision drift and interrupted integration. **Mitigated** (21 October 2026, revised after independent review of PR #40): `parent_orchestration.checkpoint` records worktree, branch, HEAD and phase at start, every phase event, worker approval, the start of integration (before it changes the parent checkout), the integrated wave and each recorded mutation. `resume` compares them with the live checkout under the lease. Any difference refuses the resume with `RESUME_CHECKPOINT_DRIFT` and the expected and actual values, records the check, releases the lease just taken and leaves the run `IN_PROGRESS` for an operator (restore the checkout and resume, or take the free lease and record an outcome). A record without a checkpoint is refused the same way with `RESUME_CHECKPOINT_MISSING`. Nothing is re-based or closed automatically. Regressions cover a changed HEAD, a switched branch, a moved worktree, an interrupted integration, a legacy record and a run finished between status and lease. Open: uncommitted working-tree state is not compared, and there is no journal of which integration steps completed, so an interrupted integration is detected but not reconciled. |

## Capability assessment

| Capability | Result | Evidence and limit |
| --- | --- | --- |
| Initialization, inspection, classification, deterministic routing | PASS | Routing validation covers 14 cases, 3 overrides, and 5 invalid configs; router tests use the real subprocess boundary. A real read-only planning run on a private target safely blocked with `NO_AVAILABLE_ROUTE`. |
| Turn and compaction continuity | NOT VERIFIED | Session-entry regression covers the logic with mocked Pi events. No real resumed Pi session was exercised after compaction. |
| Process interruption and restart | NOT VERIFIED | Saved-session restore, terminal-run rejection, the run lease and the resume checkpoint are tested with fake processes; no real Pi crash/relaunch cycle was exercised. |
| Repository/worktree isolation | PASS for bounded runs | A real two-worker local run wrote separate linked worktrees while the parent checkout remained clean; scope tests reject cross-repository and parent-checkout changes. |
| Backend availability and fail-closed routing | PASS | Backend mismatch/auth/version tests and the real private-target blocker show safe refusal. Recovery after availability returns is not automatic. |
| Bounded local adapter failure | PASS | Hanging and flooding subprocess regression tests exercise actual processes. Worker package limits come from external packages and need separate integration testing. |
| Autonomous dependency-ready progression | FAIL | No durable dispatcher or event consumer; mission command starts one follow-up. |
| Mandatory independent review | PASS at finalization | A2 gate: `COMPLETED` requires a completed worker for every independent-review step of each routed phase. Relies on the parent recording workers as the prompt requires; reviewer identity separation is not checked. |
| Review rejection, focused repair, and re-verification | NOT VERIFIED | Provenance can represent a review → repair → verification sequence; no runtime gate requires or bounds it. |
| CI at useful stages | NOT VERIFIED | Prompt guidance exists; no runtime CI stage gate or deduplication is present. |
| Complete autonomous workflow | NOT VERIFIED | The prior real two-worker run completed a bounded mutation, with no independent review or repair. No full unattended implementation → review → repair → verification run was observed. |

The real bounded runs were a read-only planning run blocked by backend
availability and a run with two isolated mutating workers that completed. Their
records are kept in local orchestrator state, not in this repository. They establish specific boundaries only; neither proves a full
autonomous delivery loop.

## Adversarial coverage and remaining integration work

Tests cover premature finalization, follow-up/compaction restoration, terminal
run refusal, cancellation, subprocess hang/output flood, concurrent parent
events, missing installed resources, GitHub auth failure, invalid routing
configuration, backend mismatch, linked-worktree isolation, concurrent parent
updates from separate processes, and pinned launcher revisions. The review-rejection test proves the trace can
record rejection, repair, and verification; completion now requires the
review step itself, but not a bounded repair/re-verification sequence. Simultaneous
resume is now covered with real competing processes against one run (#32).
Worker crash, actual Pi restart, CI gating, and transient backend recovery
still require integration tests with fake backends and isolated temporary
repositories. No unrelated
target repository or GitHub state was mutated, and no extra Pi model run was
used for this audit.

## Verification

- `npm run check`: type checking passed; 72 TypeScript tests passed, including
  external-installation integration tests.
- `python3 -m pytest -q`: 418 tests and 214 subtests passed.
- The A2 and A9 regressions were confirmed to fail against the code without
  their fixes.
- `python3 tools/validate_routing.py`: 14 routing cases, 3 override cases, and
  5 invalid-configuration examples passed.
- `git diff --check`: passed.

The direct audit trace (kept in local orchestrator state) was finalized
`PARTIAL` with blocker `AUTONOMY_GAPS`, three completed phases, eight changed
repository files, and no GitHub mutations. No `route_task` result or worker
execution was observed in this direct audit, so the trace records neither.
Follow-up on 14 October 2026 fixed A2 (reviewer-presence gate) and A9.
Follow-up on 21 October 2026 (#32) mitigated A3 (single-owner run lease) and
A10 (verified resume checkpoint); the open parts are listed in their rows.
A1 remains open and keeps the decision at NO-GO for unattended multi-step
delivery.
