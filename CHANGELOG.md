# Changelog

All notable changes to PICO are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

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
- `CONTRIBUTING.md` with the development checks, the rules for routing and
  policy changes, pull request expectations and the licensing stance.
- `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1).
- `.github/CODEOWNERS`, and issue-form contact links for security reports
  and questions.
- A newcomer path at the top of the README: what PICO and Pi are, a diagram,
  prerequisites, a documentation index, and links to the contributing,
  security and conduct files.

### Security

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
