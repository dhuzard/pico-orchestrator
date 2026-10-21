# Changelog

All notable changes to PICO are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- The lead records less (issue #43). The prompts no longer ask for `directive` or
  `feedback` interaction events or for optional phases such as `inspection`; `decision`,
  status, bind, worker/phase outcomes, the integrate bracket and finish stay. Worker
  contracts open with a `Goal:` line.
- The extension's `activity` entries carry what those events carried: at start the routed
  `route`, a `prompt_digest`, the contract's `goal` (200 characters) and a 300-character
  `prompt_excerpt` taken after the task tag and goal line; on return a 600-character
  `result_summary`, its `result_digest` and any reported `commit_sha`. Text is single-line
  with control characters removed; credential shapes (tokens, JWTs, `password=` style
  assignments with a 16+ character value, URLs with credentials, cloud and package-registry keys) are masked in the
  whole text before it is cut, and an unbroken run left at the cut is dropped. The full
  text lives in the Pi session transcript. Garden's Conversation view shows these entries.
- A routed task started with no worker running and no wave declared gets its own implicit
  single-task wave (never grown). Only an explicit `declare_wave` lets tasks run
  concurrently (`WAVE_UNDECLARED`, write-scope overlap and the parallel limit are
  unchanged). `integrate_begin`/`integrate_end` accept `"auto"` for the one open implicit
  wave and refuse with `WAVE_AUTO_AMBIGUOUS` (several) or `WAVE_UNKNOWN` (none). A run that
  ends without COMPLETED marks its open waves `abandoned` (`reconciled`, kind `wave`).
  COMPLETED is refused with `PARENT_WAVE_INTEGRATING` while an integration was begun and not
  ended, and with `PARENT_WAVE_NOT_INTEGRATED` for any task with a bound worktree that no
  integrated wave has covered since (`phase.workspace_bound`, checked per task so a missing
  wave record cannot hide it; `phase.bound_epochs` keeps the route epochs that bound one, so
  an escalation does not forget the old route's worktree); read-only tasks do not block it.
- Only a mutating step starts an implicit wave, and only when its task has no open wave in the
  current route epoch: a read-only specifier or reviewer never adds one, so after integration
  the phase keeps pointing at the integrated wave. A task that mutates again after integration
  starts a new wave.
- `benchmark_metrics.py --compare` matches waves by their task set and route epoch (raw ids kept
  as `ids` metadata) and integrate facts by that key, so `wave-1` and `auto-...` are the same
  wave. Optional phases (like `inspection`) are listed under
  "Dropped optional phases" (`provenance.dropped_optional_phases`) instead of as missing facts, but only
  for the allow-listed `inspection` phase when it was `completed` with no blocker, route, worker, wave or
  escalation; every other phase (a blocked `routing` phase, say) stays missing.
- A mutating worker that starts in an already bound worktree re-marks its task unintegrated
  (`workspace_bound`), so work done after an earlier integration whose new wave write was refused
  still refuses COMPLETED with `PARENT_WAVE_NOT_INTEGRATED`.
- The extension records a routed Codex step's `model` and `reasoning_effort` when its subagent
  starts, and its terminal status (`only_if_open`, like ClaudeWorker's) when it returns, so the
  record names the reviewing model even when the lead records no worker event. The lead's own
  event still wins.
- A failed Claude step whose retry succeeded is now `completed`: after the last failure the
  same step must have started again on the same route and returned without error. The
  failure stays in `worker_overrides` (`by` `extension` or `lead`, `reason`
  `retry_succeeded`) within the existing bounds.

### Fixed

- A run that ends PARTIAL, BLOCKED, FAILED or settled no longer leaves phases or
  workers `in_progress`. Finishing without COMPLETED sets each still-open phase
  and worker (`not_started` or `in_progress`) to `abandoned` and appends it to
  the new `parent_orchestration.reconciled` list (`kind`, `phase_id`, `step_id`,
  `from_status`, `to`, `reason`, `at`); events, activity and every other field
  stay as recorded, and a repeated finish adds nothing. COMPLETED still refuses
  open work. The lifecycle extension now records a ClaudeWorker's terminal status
  (`completed`, `failed`, or `blocked` on abort) when its result arrives, so the
  lead no longer has to; the lead's judgment wins (completed -> failed/blocked, failed -> blocked,
  or its own blocker code on a failed/blocked worker), and every such override is
  appended to the new `parent_orchestration.worker_overrides` with the status and
  blocker code it replaced. The list is bounded: a recode to a code the worker
  already had in that route epoch is refused (`PARENT_WORKER_RECODE_REPEATED`), as
  is more than 8 overrides per worker or 200 per run (`PARENT_WORKER_OVERRIDE_LIMIT`);
  neither blocks the run. The Garden shows it as "Abandoned (run ended PARTIAL)", not
  running; workers of an earlier route epoch are left as recorded. Older records still validate and display; records written by this
  version carry `reconciled` and `abandoned`, which older PICO versions reject
  (`additionalProperties: false`, status enums). Fixes #42.
- `pico workspace launch` now refuses with `WORKSPACE_BUDGET_EXHAUSTED` when the
  workspace budget is exhausted (it previously spent past it; only the
  scheduler and the Garden checked). `--ignore-budget` overrides explicitly and
  is recorded as `budget_override`. Found by the scientific-evidence-pilot run.
  Refs #6.

### Added

- Optional wall-clock limit per child execution: `pico exec start
  --max-duration SECONDS`, `max_duration_seconds` on a workspace task and a
  manifest-level default. A child that reaches it is stopped through the
  cancel path (pid start time checked), its parent run is finished `BLOCKED`
  with `EXECUTION_TIME_LIMIT`, and the execution ends `cancelled` with
  `stop_reason: EXECUTION_TIME_LIMIT`, the limit and the elapsed time. Enforced
  by a waiting launcher and by `status`, scheduler and repeat-`start` checks, with
  no background process. The scheduler and the Garden show the limit and the
  time left. The limit is per launch; a stopped execution is final, so continuing
  needs a new execution id. Compatibility: `schemas/workspace.schema.json` gains
  optional `max_duration_seconds` (top level and per task) and execution records
  gain optional `max_duration_seconds`, `deadline_at`, `deadline_epoch`,
  `stop_reason`, `time_limit`; existing manifests and records are valid and
  unchanged when the field is not set. Closes #46.

- Single-owner run lease (`tools/run_lease.py`: `acquire`, `heartbeat`,
  `release`, `status`, `takeover`). A second process that resumes a run with
  a live lease fails with `RUN_LEASE_HELD`. A stale lease is recovered only by
  an explicit takeover recorded in the run's provenance, and a holder still
  running on the same machine needs a separate force reason. Every write to a
  run (`event`, `finish`, `settle`) now requires `--owner` and is refused
  unless the caller holds the lease or is the recorded owner of a free lease.
  The lifecycle detaches and aborts running workers when its lease is lost or
  cannot be renewed in time. Garden and `pico exec status` show the holder
  (audit A3, mitigated).
- Verified resume checkpoint: the parent checkout's worktree, branch, HEAD and
  phase are recorded at each boundary, at worker approval and before
  integration. A resume whose checkout moved is refused with
  `RESUME_CHECKPOINT_DRIFT` instead of being re-based, and the run stays
  `IN_PROGRESS` for an operator (audit A10, mitigated).
- Usage accounting: Claude cache writes are recorded as `cache_write_tokens`
  and counted in budgeted spend (cache reads are not); the lead's Pi-reported
  cost is forwarded as optional `estimated_cost_usd`; usage blocks carry
  `cost_complete` and `unpriced`, shown by the scheduler and the Garden; the
  scheduler counts the spend of children that are still running, and holds
  new launches when that spend cannot be resolved. Per-participant
  `cost_complete` is false for calls with no cost or a zero cost despite
  tokens. Records written before this change stay valid and readable.
- `CONTRIBUTING.md` with the development checks, the rules for routing and
  policy changes, pull request expectations and the licensing stance.
- `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1).
- `.github/CODEOWNERS`, and issue-form contact links for security reports
  and questions.
- A newcomer path at the top of the README: what PICO and Pi are, a diagram,
  prerequisites, a documentation index, and links to the contributing,
  security and conduct files.
- Pico Garden has a read-only **Open issues** panel backed by `GET
  /api/issues`. It lists up to 50 open issues per repository (every manifest
  repository in workspace mode), with linked local `pico/issue-<n>` branches
  and closing PRs. Identity resolution fails closed, a missing,
  unauthenticated or rate-limited `gh` is shown explicitly, and results are
  cached for 60 seconds.
- `issue_reference: refs | closes` in `.pi/autonomy.yaml` (default `refs`)
  and an optional `closes_issue` on workspace tasks, so a published PR can
  say `Closes #N` and close its issue on merge. The publication gate accepts
  `Closes #N` only when the policy or the task opts in, and now refuses any
  unauthorized closing keyword anywhere in the body. The gate reads the
  base commit and the policy through the GitHub API (never local git refs
  or config), binds both into the plan hash, and refuses issues outside the
  declared ones or the PR's repository. Because the plan binds the base
  commit, a merge to the base between dry run and apply now gives
  `GITHUB_STATE_CONFLICT`, also for `Refs`-only PRs.
  PICO's own `.pi/autonomy.yaml` sets `closes`, and the pull request
  template uses `Closes #`.

### Compatibility

- Run records written by this version can contain `lead_usage.estimated_cost_usd`,
  `lead_usage.cache_write_tokens`, `lead_usage.cost_complete`,
  `worker_usage.*.cache_write_tokens` and `worker_usage.*.cost_complete`.
  Older PICO versions validate these closed objects with
  `additionalProperties: false` and will reject such records. The record
  version is not bumped.
- Run records can also contain `parent_orchestration.owner_id`, `checkpoint`,
  `resume_checks` and `lease_takeovers`, which older PICO versions reject for the
  same reason.
- An older PICO rejects a `.pi/autonomy.yaml` that contains
  `issue_reference` with `AUTONOMY_POLICY_INVALID`, because the schema does
  not allow unknown keys. Update PICO before adding the key to a repository
  that other installs also read. `.pi/autonomy.yaml` is now tracked in this
  repository, so every worker worktree inherits it, and a Garden permission
  toggle leaves a tracked modification until it is committed.

### Security

- With `pico watch --controls`, `/api/runs`, `/api/workspace` and
  `/api/issues` now need the session token too (audit S4). The viewer sends
  it on every poll.
- Claude workers receive an allowlisted environment instead of the parent's
  full environment. GitHub, SSH-agent and npm credentials are withheld unless
  named in `PI_ORCHESTRATOR_WORKER_ENV_ALLOW`, and the worker result records the
  names (never the values) of dropped variables. More bypass forms of `git`
  and `gh` are denied, and `PI_ORCHESTRATOR_WORKER_SANDBOX=1` opts into the
  Claude Code sandbox.
- The launcher runs a repository's `.pi/orchestrator.lock` only when the
  commit is on the default branch, in a release tag, or approved for that
  checkout with `pico pin`; otherwise it fails with
  `PIN_NOT_ON_RELEASE_HISTORY`. A changed pin prints a warning.
- Garden: policy writes refuse symlinked `.pi` paths, `/api/controls` and
  `/api/branches` require the controls token when controls are on, and a
  malformed `Content-Length` returns 400.
- CI actions are pinned to commit SHAs without persisted credentials, and
  CodeQL and Dependabot are configured.
- `SECURITY.md`: report vulnerabilities privately through GitHub private
  vulnerability reporting. It lists the supported versions and the
  PICO-specific scope.

### Changed

- `pico exec start --resume` needs `--takeover-stale-lease` to continue a
  crashed launch; the scheduler's `--resume-interrupted` implies it but never
  displaces a running holder.
- `tools/parent_orchestration.py start|event|finish|settle` and
  `tools/context_budget.py start|checkpoint` require `--owner`.
- An `IN_PROGRESS` run recorded before this release has no resume checkpoint:
  its resume is refused with `RESUME_CHECKPOINT_MISSING` and it stays
  `IN_PROGRESS`. Close it explicitly with `run_lease.py acquire` followed by
  `parent_orchestration.py finish --owner`, or start a new run.
- State files are written through one atomic writer (unique temp file, fsync).
  It refuses to write through symlinks, including a symlinked user model
  config.
- `pico exec` records are locked across start, status and cancel, so two
  concurrent starts launch one child, and a cancel is not overwritten when the
  child exits.
- CI also runs `tools/validate_routing.py` and `ruff`. Python 3.10 is the
  minimum supported version.
- Documentation no longer points at issue numbers from the pre-public
  repository. `backlog/backlog.yaml` notes that entries before v1.0.0 cite
  that repository and its commits.

## [1.0.0] - 2026-10-20

First public release. See `docs/releases/v1.0.0.md` for the full notes.

### Added

- Deterministic Claude/Codex routing with fail-closed backend preflight,
  capacity tiers and user-owned model profiles (`pico models`).
- The `pico` launcher: setup, update, doctor, sync, run, pin, recovery and
  uninstall. Code is cached and immutable; runtime state lives outside it.
- Parent orchestration provenance that survives turns, compaction, reload and
  restart, with trace rendering.
- Worktree-scoped workers, declared waves and serialized integration, with
  scope checks before every worker call.
- Headless execution (`pico exec`), idempotent and resumable, with checkout
  restore after each child.
- Multi-repository workspaces: manifest and planner (`pico plan`), a
  repository-scoped child launcher, and a bounded, restart-safe scheduler
  (`pico workspace schedule`).
- Cross-repository contracts and coordinated draft PRs: `validate`,
  `publish`, `ci`, `promote`, and qualification waves.
- An optional workspace token budget.
- Pico Garden (`pico watch`), with a workspace view and confirmed controls
  (`--controls`) for the workspace actions, permissions and models.
- A constrained, audited GitHub adapter for issues and PRs, with dry-run plan
  hashes and non-force publication.

### Security

- Garden actions require a per-session token, a matching Host and Origin, and
  a confirmation bound to the previewed state.
- Publication requires the autonomy policy, or a one-time grant recorded with
  the publication.
- Workers may only write inside their bound worktree, never inside `.git`.

[Unreleased]: https://github.com/dhuzard/pico-orchestrator/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/dhuzard/pico-orchestrator/releases/tag/v1.0.0
