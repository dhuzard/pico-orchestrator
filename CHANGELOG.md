# Changelog

All notable changes to PICO are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `CONTRIBUTING.md` with the development checks, the rules for routing and
  policy changes, pull request expectations and the licensing stance.
- `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1).
- `.github/CODEOWNERS`, and issue-form contact links for security reports
  and questions.
- A newcomer path at the top of the README: what PICO and Pi are, a diagram,
  prerequisites, a documentation index, and links to the contributing,
  security and conduct files.
- `issue_reference: refs | closes` in `.pi/autonomy.yaml` (default `refs`)
  and an optional `closes_issue` on workspace tasks, so a published PR can
  say `Closes #N` and close its issue on merge. The publication gate accepts
  `Closes #N` only when the policy or the task opts in, and now refuses any
  unauthorized closing keyword anywhere in the body. The gate reads the
  policy at the base commit the GitHub remote reports (never a local ref),
  binds that commit and the policy blob into the plan hash, and refuses
  issues outside the declared ones or the PR's repository.
  PICO's own `.pi/autonomy.yaml` sets `closes`, and the pull request
  template uses `Closes #`.
- Compatibility: an older PICO rejects a `.pi/autonomy.yaml` that contains
  `issue_reference` with `AUTONOMY_POLICY_INVALID`, because the schema does
  not allow unknown keys. Update PICO before adding the key to a repository
  that other installs also read. `.pi/autonomy.yaml` is now tracked in this
  repository, so every worker worktree inherits it, and a Garden permission
  toggle leaves a tracked modification until it is committed.

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
