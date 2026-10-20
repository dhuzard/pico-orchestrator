# v1.0 release and reusable-project checklist

Use this as a human-reviewed release gate. The orchestrator can classify and
coordinate work, but release decisions and GitHub mutations remain explicit
operator actions.

## End-to-end task workflow

1. **Classify the request.** Capture the task fields required by
   `schemas/task.schema.json`, acceptance criteria, affected areas, dependencies,
   and relevant target-project constraints. Read repository instructions such as
   `AGENTS.md`; let `route_task` load and validate `.pi/routing.yaml` rather
   than parsing or applying project risk floors yourself. Do not infer missing
   facts or edit policy to force a desired route.
2. **Ask `route_task` for the decision.** Run the Pi extension/tool with the
   classified task and project-local routing config. Check `ok`, base/effective
   classification, selected route, execution plan, capacity assignments, and
   backend bindings. On `ok: false`, repair classification/config and call it
   again; never choose a route by prompt or model preference.
3. **Execute the authored plan.** Follow step order/dependencies, fresh-context
   and independent-review requirements, capacity, permissions, and the
   project's validation commands. For delegated mutations, use the documented
   pinned-commit, isolated-worktree protocol and explicit path ownership. Keep
   reviewers read-only. Do not let workers push, merge, deploy, or mutate
   GitHub.
4. **Record the provenance trace.** Create/update a run with the actual stored
   `route_task` result, starting/ending commit, step/agent attribution, returned
   artifact references, validation outcomes, review findings, and observable
   telemetry. Record unavailable values as `null`/unknown; never fabricate
   tokens, costs, outcomes, or transcript content. Verify the user-facing
   report with `python3 tools/backlog.py trace <run-id>` (or `--latest`) and
   retain the trace path.
5. **Validate the change.** Run relevant project checks and the orchestrator
   suite: `python3 tools/validate_routing.py`,
   `python3 tools/backlog.py validate`, `npm test`, `npm run typecheck`, and
   `python3 -m pytest` (or
   `python3 -m unittest discover -s tests -p 'test_*.py'`). Record exact
   commands/results in provenance. Resolve failures or clearly report anything
   not run; do not label a partial run as passed.
6. **Branch and PR.** After reviewing the integrated diff and successful
   acceptance evidence, create/push the branch through an authorized human or
   CI workflow, then create/link a PR. `github-pr-create` requires an already
   pushed head branch and uses a non-closing `Refs #N` body. The orchestrator
   does not push or merge.
7. **Close the GitHub issue deliberately.** Complete the local backlog item
   only once its acceptance criteria are met and required review/validation are
   complete. Then run `python3 tools/backlog.py github-push ORCH-NNN`; its close
   guard permits issue closure only for a locally `completed` item. Confirm
   remote state and resolve reported sync conflicts explicitly.

## Reusable-project release gate

- Require repository governance before release: `main` must require pull
  requests and the CI status checks, with force pushes and branch deletion
  disabled. Do not release from an unprotected default branch.
- Pin a reviewed tag/commit for repeatable use; do not assume `main` is a
  release artifact. For target projects, `pico pin .` writes the reviewed
  commit to `.pi/orchestrator.lock`; `pico update .` is the explicit action
  that advances an existing lock.
- In a clean target project, bootstrap `pico`, run `pico setup .`,
  `pico doctor . --json`, and `pico run .`. Install Python/npm development
  dependencies where repository validation is needed.
- Keep target-specific instructions in `AGENTS.md` and constraints-only
  routing additions in `.pi/routing.yaml`. Verify the target configuration
  against the project-routing schema; ensure it does not attempt to replace
  global policy.
- Run the smoke matrix before tagging:
  - **fresh repository:** `pico sync <fresh-repo> --github-templates`, then
    verify generated configuration and a successful doctor;
  - **existing repository:** run the same command against a repository with
    project-owned `AGENTS.md`/GitHub templates and verify they are preserved;
  - **pinned repository:** `pico pin .`, advance upstream, verify `pico run .`
    stays pinned, then verify `pico update .` advances the lock;
  - **recovery:** dirty the cached checkout deliberately, verify ordinary update
    fails closed, then recover with `pico reset-cache --force` while state is
    preserved;
  - **uninstall:** verify `pico uninstall` removes only the launcher and that
    purge flags remove cache/state/config only when explicitly requested.
- Run all checks applicable to the target and orchestrator, inspect the
  provenance trace, review the final diff, and document any skipped command or
  environment-specific limitation before tagging a release.
- Run `npm audit --audit-level=high` and triage every high/critical finding.
  A finding may be accepted only with a documented dependency path, actual
  runtime exposure analysis, and follow-up owner; never use
  `npm audit fix --force` as an unreviewed release step.
- Include a trace-backed example in user-facing docs. The rendered sample in
  `README.md` demonstrates route choice, agent organization, artifacts, and
  validation without relying on live model output.

## Operational recovery and uninstall

Cached `pico` execution separates code from mutable state:
`~/.cache/pi-coding-orchestrator/repo` contains the Git checkout and
`~/.local/state/pi-coding-orchestrator/runs` contains runtime provenance.
A cache recovery must never delete state implicitly.

Release verification must exercise:

```sh
pico doctor . --json
pico state-path
pico reset-cache --force
pico uninstall
```

The v1.0 supported launcher environments are Linux and macOS POSIX shells and
Windows through WSL2 (recommended) or a compatible Git-Bash environment. Native
PowerShell is not a supported launcher surface for v1.0.

## Explicit limitations and follow-up

- Worker token telemetry is provider/backend-dependent. Persist structured
  usage only when observed; unsupported or absent usage remains unknown.
  Follow-up: add provider-specific adapters/tests as supported backends expose
  stable usage metadata, without making telemetry a routing input.
- Cost estimation is intentionally unavailable without a reviewed,
  versioned pricing table that accounts for model and cached-token rates.
  Follow-up: propose and review such a table before reporting estimated costs;
  never infer price from token counts alone.
- There is no autonomous merge or deploy behavior. PR creation is explicit and
  merge/deployment remain under human or separately authorized CI control.
  Follow-up: keep any future automation behind separate narrowly scoped
  authorization, safeguards, and review; do not broaden worker permissions.
