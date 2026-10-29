# Changelog

All notable changes to PICO are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `pico workspace init` builds a workspace manifest: it discovers the Git checkouts below a root, lets
  you select 2 to 64 of them, verifies each read-only on GitHub, previews the manifest with the planner's
  waves and blockers, and writes a new file only after `yes` (or `--spec FILE --approve`). It never
  overwrites, clones, fetches or launches. Directory names, Git and GitHub answers and errors are shown
  with control, bidirectional and other invisible characters escaped, so a crafted directory name cannot
  inject terminal sequences or fake lines; raw paths still reach Git and the saved YAML. Command-line
  errors escape the arguments they quote, a malformed Git remote URL is reported for its repository
  (`WORKSPACE_INIT_REMOTE_MALFORMED`) without stopping the others, and numeric answers such as `²` are
  re-asked. See `docs/workspace-interactive-design.md`.

### Changed

- Workspace tasks are checked for mutation authorization before launch, and waiting for a human's
  approval is no longer reported as a failure. On scientific-review-four-repo-proposal-v1, two
  upstream tasks in repositories without `.pi/autonomy.yaml` spent lead tokens, then stopped `BLOCKED`
  with `HUMAN_MUTATION_APPROVAL_REQUIRED`. They had no worker and made no mutation. Their five
  downstream tasks were shown as `blocked_by_failure`. Now:
  - An implementation task needs `create_worktrees` and `commit` from the policy committed at its
    pinned commit, plus `--allow-mutation`, or it is not launched. An untracked policy does not count,
    and a launch with `--allow-mutation` is refused (`AUTONOMY_POLICY_UNCOMMITTED`) while the
    checkout's policy file differs from the committed one, also for a `mutation: none` task.
  - Such a task is `awaiting_authorization` and its dependents are `waiting_for_authorization`. The
    scheduler prints one approval request per repository and exits 3. The Garden shows the same
    requests.
  - A child that stopped asking for approval counts the same way, but only when its run shows no
    mutation, worker, spawned agent, child run or bound worktree, and its recorded checkout is clean,
    at its starting `HEAD` and branch, with no local branch moved (the execution's `checkout` block
    now records `head_before`, `head_after` and `branches_changed`). A refused resume of an interrupted
    execution stays a refusal.
  - The Garden's guarded `approve_mutation` action grants only those two permissions, after an exact
    diff preview. It never grants push, PR, merge or deploy.
  - After the policy is committed and re-pinned, the next schedule starts a new attempt
    (`<workspace>--<task>--attempt-<n>`) linked to the blocked one, whose records are left as they
    were. A run that asks again under the same authorization is not retried.
  - New: `pico workspace preflight` and the manifest field `mutation: none` for read-only tasks.
  - Breaking: a workspace whose repositories have no committed policy granting local mutation no
    longer launches anything. The synthetic benchmark setup now always commits one.

### Documentation

- Added `docs/containment-and-authority.md`: containment acceptance tests for worker sandboxing (#63),
  the OS sandbox versus command filter distinction, and planning, execution, publication and
  promotion authority for the issue dispatcher (#34).

### Security

- Mutating Claude workers run in Claude Code's OS sandbox by default (#63, slices 2 to 4).
  Their shell commands may write only in the bound worktree, its git metadata, the temp
  directory and cache directories the repository declares; cannot read PICO's state directory
  (run records, leases), `~/.config/gh`, `~/.ssh` or other credential files (also at their
  resolved location when they are symlinks); and have no network. Claude's file tools get
  `permissions.deny` Read and Edit rules for the same paths. `sandbox.allowUnsandboxedCommands:
  false` stops unsandboxed retries and a repository's `.claude/settings.json` from widening the
  sandbox. Every Claude worker, read-only reviewers included, runs with `disableAllHooks` and
  `--strict-mcp-config`, so hooks and MCP servers planted in a repository (`.claude/settings.json`,
  `.mcp.json`) do not run. A repository can allowlist hosts and cache directories in a new,
  schema-validated `worker_sandbox` section of `.pi/autonomy.yaml`, read only from the default
  branch on GitHub like publication permissions (never from the checkout, which worker commits
  reach) and pinned at run start: a resume can narrow it, never widen it. A failed read widens
  nothing. It cannot turn the sandbox off. The posture is recorded on every run in
  `parent_orchestration.worker_sandbox` (with the policy commit, any refused widening, and which
  widening keys the user's own Claude Code settings set) and on each `ClaudeWorker` result
  (`sandbox`).
  Not covered: the user's own `~/.claude/settings.json` (`excludedCommands`, `allowWrite`,
  `allowedDomains`) still merges into workers' settings and can widen the sandbox (recorded,
  not refused); declared cache directories are the user's real caches and can be poisoned;
  Codex workers and the lead (slices 5 and 6); platforms without the sandbox; and per-attempt
  denial records (Claude Code reports sandbox denials to the model, not in the JSON result PICO
  reads). Not yet measured with real agents (slice 1).

  **Migration.** `PI_ORCHESTRATOR_WORKER_SANDBOX` changes meaning: unset now means on (it was
  off), `1` stays on, and `0` is the explicit opt-out, recorded on the run as
  `state: opted_out`. On Linux and WSL2 install `bubblewrap` and `socat`; PICO checks that both
  are on the worker's PATH and that bubblewrap can actually create its namespaces (on Ubuntu
  24.04 and later it may need an AppArmor profile; see Claude Code's sandboxing docs). Without
  that, a mutating Claude worker is not started and fails with the new prerequisite code
  `WORKER_SANDBOX_UNAVAILABLE`. Native Windows has no sandbox; use WSL2 or opt out. Route
  preflight now requires Claude Code 2.1.285 or later
  (`claude_code_worker_sandbox_minimum_version` in `routing/backend-compatibility.json`) unless the
  sandbox is opted out, with `MODEL_BACKEND_VERSION_UNSUPPORTED` (`requirement: worker_sandbox`).
  Checks that need the network (installs, registries) need
  `worker_sandbox.network.allowed_domains` on the default branch, or dependencies installed
  before the worker starts. Read-only Claude steps get no sandbox, only hooks and MCP off and the
  deny rules. Run records now carry `parent_orchestration.worker_sandbox`, which older PICO
  versions (closed schema) reject.
- Issue and comment writes are enforced (#58). `github_integration.execute` and
  `backlog github-push` read `create_github_issues`, `comment_on_issue` and `comment_on_pr`
  (and `create_pr` for `update_pr`) from `.pi/autonomy.yaml` on the default branch on GitHub,
  through the same API path as publication, and refuse with `GITHUB_WRITE_NOT_AUTHORIZED`
  before any write; `execute` refuses the dry run too. No override exists.
- Removed the Garden one-shot publication grant (#59). Whoever held the session token, an agent
  included, could start its own `pico watch`, read the token and confirm a grant that the record
  showed as `granted_by: garden`. Publication now needs `push_branch` and `create_pr` in the
  default branch's policy. A Garden request that still sends `grant_once` is refused with
  `CONTROL_BODY_INVALID`. Worker sandboxing, which would stop an agent going around PICO, is #63.

- Publication permissions come from the default branch's policy on GitHub (issue #39).
  `github_integration.publish` took `push_branch` and `create_pr` from the checkout's own
  `.pi/autonomy.yaml`, which a worker can edit, commit on its `pico/` branch (it reaches the
  integrated branch on integration) or symlink. It now uses the single API-read policy on the
  repository's default branch (`repos/<repo>` `default_branch`, a new allowlisted, host-pinned
  read) that also decides `issue_reference`. The caller's `--base` only chooses where the PR
  points and is never authority, so a `pico/` branch on GitHub cannot authorize itself. The
  working tree is never an authority.
- The default branch is now the root of trust for publication: protect it (required review, no
  direct pushes). Agent-side tool denylists do not fully stop a same-user process (S1, #59).
- Removed `backlog github-pr-create`: it ran `gh pr create` for any base and head with no
  permission check, dry run or plan hash. Open PRs with `github_integration --publish` (or
  `pico workspace publish`) and link them with `github-pr-link`.

### Changed

- CI runs the Python suite in parallel with `pytest-xdist` (`-n auto`). The suite is bound by its
  subprocesses, so on four cores it takes about 2.5 minutes instead of 7.5. Local runs can do the same
  with `pip install pytest-xdist` and `pytest -n auto`.
- Behavior change (#58): with the `manual` profile, where every permission is off, backlog sync
  (`github-push`), issue comments, issue edits and closing an issue on backlog completion are
  refused until the repository's default branch enables `create_github_issues`,
  `comment_on_issue` or `comment_on_pr`. Set them in `.pi/autonomy.yaml`, commit it and merge it.
- Behavior change (#59): the Garden no longer offers "Allow push and PR for this PR only".
  Publication records carry `authorization: {source: policy, permissions, policy_commit}`;
  `one_shot_grant` appears only in older records.
- Behavior change: a local-only permission edit no longer authorizes publication. Uncommitted,
  locally committed or `pico/`-branch changes to `.pi/autonomy.yaml` are ignored for
  `push_branch`/`create_pr`; the policy must be on the default branch on GitHub. A missing
  policy is `PUBLICATION_NOT_AUTHORIZED`. A symlinked
  `.pi` or policy in the checkout is refused with `AUTONOMY_POLICY_PATH_UNSAFE`; a dirty copy is
  ignored. The plan records `policy_branch`, `policy_commit`, `policy_blob` (always; replaces
  `issue_policy_blob`), `publication_permissions` and `base_commit`, all bound by the plan hash.
  `issue_reference_decision` reports `policy_branch`/`policy_commit` instead of `base_commit`.
  `publish` also returns `policy_drift` (informational list of permissions where the checkout
  copy differs). The Garden permission panel and `set_permission` preview say a toggle does not
  affect publication until committed and pushed to the default branch.
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
- A lead that restates a worker the extension already recorded (the old habit for Codex steps) is
  never an error: the same model with or without its provider prefix, a different
  `reasoning_effort` (the routed plan's value is authoritative) and an `in_progress` after the
  automatic terminal status are accepted as no-ops (`worker.auto_recorded`), and any remaining
  refusal for such a worker never blocks the run. Codex workers now follow the ClaudeWorker rule in
  the prompts: record one only when your judgment differs or to add a `blocker_code`.
- A failed Claude step whose retry succeeded is now `completed`: after the last failure the
  same step must have started again on the same route and returned without error. The
  failure stays in `worker_overrides` (`by` `extension` or `lead`, `reason`
  `retry_succeeded`) within the existing bounds.

### Fixed

- A task can be published again after its draft PR was closed without merging (#67). Its branch
  stayed refused with `PUBLICATION_PR_CONFLICT` for good, and the #4 pilot had to delete and recreate a
  repository. `pico workspace publish --new-branch` (on both the dry run and `--apply`) pushes the
  validated commit to the next free `<head>-<n>` branch, with a draft PR that names the closed PRs it
  supersedes. The closed PR's branch is never reused, reopened or overwritten, a merged PR is never
  superseded, and without the flag the refusal stays (its message now names the flag). A branch with
  another task's head or anyone else's PR is skipped, never taken over, and a task already released to
  downstream goes back to `validated` until it is promoted on the new PR's CI.
- A resumed headless run no longer stops at its required re-route (#69). In the #4 pilot the resumed lead
  called `orchestration_record status` and got "BLOCKED: rerun route_task ...", and the resume prompt
  told it to stop on BLOCKED, so it did, and the run ended `ORCHESTRATION_OUTCOME_UNRECORDED`. That status
  reply now begins `NEXT STEP (PARENT_ROUTE_RECHECK_REQUIRED): the run is active and not stopped`. The
  resume prompt and the orchestrate prompts name it as the expected first step of a resume.
- A task no longer launches on a parent checkout left dirty by the previous run (#68). In the #4
  pilot a lead applied its integrated diff to the parent checkout, and the next task in that
  repository launched on it because only `HEAD` was checked. `workspace launch` and scheduler
  dispatches now refuse a checkout with uncommitted tracked changes with `WORKSPACE_CHECKOUT_DIRTY`,
  naming the files and the run that left them. Each execution's `checkout` block records
  `dirty_files` and `dirty_count`. Nothing is cleaned automatically.
- Each execution's `checkout` block also reports untracked entries that appeared in the parent
  checkout during the run (`new_untracked`, `new_untracked_count`; `.pi/worktrees/` and ignored
  files excluded). They are a report only, so they never refuse a launch, but they show a write
  outside the run's worktrees (#63).
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

- The synthetic benchmark kit can drive the sandbox run of #4. `run_real.py` passes
  `--publication`, `--i-own-these-repos` and `--issue` to setup and, in that mode, keeps the
  repository access check of `pico workspace schedule` (a missing repository fails closed);
  `--continue` reuses the recorded mode. `setup.py` and `run_real.py` take `--max-tokens` for a
  bounded budget. The qualification task now checks both repositories at their validated commits.
- Fixed: the synthetic `synth-lib` CI workflow ran the whole suite, including `test_stopwords`, which
  can never pass, so `lib-slugify`'s draft PR could never be CI-green and nothing downstream could be
  promoted in a real sandbox run. It now runs the slugify tests; `synth-app` keeps its whole suite.
  Found by the first real run of #4; a kit test now runs each workflow's command on the reference solution.
- The synthetic pilot as the main regression benchmark (issue #49), in two separate parts.
  `tests/test_synthetic_pilot.py` is a deterministic integration suite that runs in CI on every
  pull request, with no model, network or tokens: the synthetic workspace goes through the real
  scheduler, launcher, run lease, recorder, contract gates (a fake GitHub and bare repositories) and
  `benchmark_metrics`, with a scripted fake lead (`tests/scenario_lead.py`) in place of the model.
  Eight scenarios cover a concurrent wave over two repositories with publish, green CI and promotion,
  the dependency gate, PARTIAL reconciliation with `PREREQUISITE_UNCHANGED`, in-flight budget holding
  and the direct-launch gate, a SIGKILLed child resumed under the lease (and a drifted checkout
  refused), `EXECUTION_TIME_LIMIT`, the lead-call limit's resumable stop and `REPEATED_INEFFECTIVE_ACTION`.
  Each compares its provenance with a golden record in `tests/fixtures/golden/`
  (`PICO_UPDATE_GOLDEN=1` regenerates it deliberately and is refused when `CI` is set). The
  real-agent benchmark stays separate and explicit: `benchmarks/synthetic-workspace/run-real.sh REV
  LABEL` refuses to run unless `PICO_REAL_BENCHMARK=1`, runs setup, launch, validate and metrics on a
  pinned revision and writes a JSON report; CI never sets that variable. CI runs the suite as its own
  step. Not covered by the deterministic suite: what a model actually records, the TypeScript
  extension's enforcement, real GitHub, and the merging of integrated waves.
- Detection of repeated ineffective actions (issue #48). A failed worker step is recorded as
  a structured failure signature in `parent_orchestration.failure_signatures`: (task, step,
  route epoch, route, failure code, checkout HEAD, a digest of the declared inputs). The code
  is the worker result's own `blocker_code` (else `WORKER_FAILED`); no free text is parsed.
  Before the lifecycle extension starts a `ClaudeWorker` or `spawn_subagent` step it asks the
  recorder (`retry_gate`): if one signature at the current HEAD and inputs has failed more
  often than the per-signature retry budget allows (default 2 retries, so the third failure
  exhausts it), the start is refused with `REPEATED_INEFFECTIVE_ACTION`, the step is recorded
  `blocked` with that code and the lead is told why. The refusal never sets `parentBlocked`:
  the phase, the run and every other task continue, and the lead may finish PARTIAL or
  BLOCKED (reconciled as usual). A new commit, changed inputs, or a different route epoch is
  a different signature, so the count starts again; a step the refusal blocked is reopened
  as failed when the state changed; a successful retry (#54) clears the step's signatures.
  A prerequisite blocker is recorded as blocked, not looped on: a code in the closed set
  `{APPROVED_PILOT_INPUTS_UNAVAILABLE, PREREQUISITE_UNMET, MODEL_BACKEND_UNAVAILABLE,
  MODEL_AUTH_UNAVAILABLE}`, or a worker result with `blocker_kind: prerequisite` (no naming
  pattern is trusted, because the code is worker-chosen) blocks the step immediately with its
  own code and never counts toward `REPEATED_INEFFECTIVE_ACTION`. Such a step cannot be
  restarted until its HEAD or declared inputs change: the start is refused with
  `PREREQUISITE_UNCHANGED` (not called a loop; it never blocks the run), and a changed state
  reopens the step. An unknown HEAD (lookup failed) counts as no state change. For an unbound
  step (a read-only reviewer) the HEAD is that of the task's bound mutating worktree, else the
  main checkout. This replaces the original issue's "finish the run BLOCKED" criterion, per
  the maintainer's refinement: the step is blocked, not the run, so other tasks continue;
  repeated refused starts are bounded by the lead-call limit (#47) rather than by ending the run.
  The declared-inputs digest covers pins, upstream readiness and validated commits of a
  workspace dispatch, not the status of approved input files, and is absent in a single-repo
  `pico exec`, where only HEAD resets the count (a follow-up could digest the declared input
  paths). The budget is
  `failure_retry_budget` (0 to 1000; 0 allows no retry): `pico exec start
  --failure-retry-budget N`, a workspace task, a manifest default, or
  `limits.failure_retry_budget` in `.pi/autonomy.yaml` (precedence: CLI/task, manifest,
  repository policy, default 2). Workspace launches also pass a digest of the declared
  dispatch inputs (`PI_ORCHESTRATION_INPUTS_DIGEST`). The signature list is bounded (100;
  the oldest unblocked entry is dropped first). Compatibility: `schemas/workspace.schema.json`
  and `schemas/autonomy.schema.json` gain the optional `failure_retry_budget`; the run record
  gains optional `parent_orchestration.failure_signatures` and `failure_retry_budget`, and
  the `worker_overrides[].reason` enum gains `prerequisite_blocker`,
  `repeated_ineffective_action` and `state_changed`; execution records gain optional
  `failure_retry_budget`, `failure_retry_budget_source` and `inputs_digest`. Existing
  manifests, policies and records are valid and unchanged. Failures of `pico workspace
  validate` checks are not yet fed into the signatures. Closes #48.

- Optional maximum lead calls per run (issue #47), with a warning and a resumable stop.
  `max_lead_calls` is set per workspace task, as a manifest default,
  `pico exec start --max-lead-calls N`, or as `limits.max_lead_calls` in
  `.pi/autonomy.yaml` (precedence: CLI/task, then manifest, then repository policy; unset
  means no limit). `lead_calls_warn_at` (same levels, `--lead-calls-warn-at`) defaults to
  80% of the limit, rounded down; there the extension tells the lead once to converge and
  records `lead_call_warning`; nothing is blocked. At the limit only `orchestration_record`
  `status` and `finish` stay available, and the lead's finish within a grace of 2 more
  calls is honoured as given. Otherwise the run is not finished: the extension records
  `lead_call_limit_reached` (fresh resume checkpoint, `stopped_reason: LEAD_CALL_LIMIT`),
  accounts for in-flight workers, releases the lease through the owner path and aborts the
  agent. The run stays `IN_PROGRESS`; the headless execution ends `interrupted` with
  `stop_reason: LEAD_CALL_LIMIT`. Resuming needs an explicit higher limit
  (`--resume --max-lead-calls N`, or a higher manifest/task limit with
  `--resume-interrupted`); the same or a lower limit is refused with
  `LEAD_CALL_LIMIT_EXHAUSTED`. The call count carries over a resume. Garden and the
  scheduler show lead calls against the limit; the benchmark metrics report carries the
  limit. Compatibility: `schemas/workspace.schema.json` gains optional `max_lead_calls` and
  `lead_calls_warn_at` (top level and per task), `schemas/autonomy.schema.json` gains
  optional `limits.max_lead_calls` and `limits.lead_calls_warn_at`, the run record gains an
  optional `context.lead_call_limit` and checkpoint reason `lead_call_limit`, and execution
  records gain optional `max_lead_calls`, `max_lead_calls_source`, `lead_calls_warn_at`.
  Existing manifests, policies and records are valid and unchanged when nothing is set.
  Closes #47.

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
