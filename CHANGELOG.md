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

### Security

- `SECURITY.md`: report vulnerabilities privately through GitHub private
  vulnerability reporting. It lists the supported versions and the
  PICO-specific scope.

### Changed

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
