# PICO - Pi Coding Orchestrator

Reusable Pi setup for higher-quality multi-agent coding runs and audits. It
loads a deterministic Claude/Codex router, gives an orchestrator prompt a clear
execution protocol, and can record traceable run provenance.

Use it when you want to work inside an existing repository with Pi, but want a
more structured agentic workflow than a single ad-hoc chat.

## Quickstart: use from any repo

Install the short `pico` launcher once, then use it from any repository:

```sh
curl -fsSL https://raw.githubusercontent.com/dhuzard/pico-orchestrator/main/scripts/use-latest.sh | bash -s -- install

cd existing-project
pico setup .
pico doctor .
pico run .
```

Inside Pi, start an authorized parent run with `/orchestrate <bounded engineering
objective>`. Merely opening `pico run .` does not start one: the
`orchestration_record` tool rejects writes until that command initializes a
parent. Continue in the same saved Pi session to retain its run identity; an
explicit session resume restores the active record after a reload. A new
session needs a new `/orchestrate` command. Use `pico run . --repo
owner/repository` when GitHub remotes are ambiguous.

Read the script before piping it to a shell if you prefer: it only clones this repository into the cache and installs `pico`. With the GitHub CLI, the same bootstrap is `gh api -H "Accept: application/vnd.github.raw+json" repos/dhuzard/pico-orchestrator/contents/scripts/use-latest.sh | bash -s -- install`, which also works for a private fork. The GitHub CLI is still needed, authenticated, for PICO's GitHub features (issues, draft PRs, CI checks).

The launcher installs a command named `pico`. Many systems also ship the `pico` text editor in `/usr/bin`; while `~/.local/bin` comes first on your `PATH`, `pico` runs PICO, and the editor stays available as `/usr/bin/pico` (or `nano`). By default `pico` is installed in `~/.local/bin`; add that directory to `PATH` if your shell does not already include it. The launcher keeps immutable code in `~/.cache/pi-coding-orchestrator/repo` and mutable runtime state in `~/.local/state/pi-coding-orchestrator`, so provenance cannot dirty or block updates of the cached Git checkout.

The launcher keeps a Git checkout that tracks the default branch in
`~/.cache/pi-coding-orchestrator/repo`. Every command runs from an immutable
snapshot of the resolved commit in `~/.cache/pi-coding-orchestrator/runtimes/<commit>`.
Sessions pinned to different commits can therefore run side by side, and
updating one never changes the code under another. A project pin whose commit is
already cached runs offline. Without Claude Code installed, `pico run` still
starts: routes with a Claude step report `MODEL_BACKEND_UNAVAILABLE` and are
rerouted when the policy allows it.

What those commands do:

- `pico setup .` conservatively initializes the target repo. It writes starter
  `AGENTS.md`, `.pi/routing.yaml`, `.pi/autonomy.yaml`, and
  `.pi/orchestrator-pr-workflow.md` only when needed. Add
  `--github-templates` to seed missing professional issue/PR templates without
  replacing existing project templates.
- `pico doctor .` checks the target plus git, Python, Pi, GitHub CLI/auth, Claude Code/auth, effective model profiles, state path, orchestrator revision, and any project pin. Add `--json` for machine-readable output.
- `pico run .` refreshes the cached orchestrator and opens Pi with the router, workflow, mission-controller, and lifecycle extensions loaded.
- `pico sync .` combines refresh + conservative setup + doctor for an existing repository.
- `pico pin .` writes the exact orchestrator commit to `.pi/orchestrator.lock`; later `pico run .` honors that reviewed commit. `pico update .` explicitly advances an existing lock to the resolved update revision.

To report a PICO bug while working in any repository, write a short report and
preview the issue before sending it to `dhuzard/pico-orchestrator`:

```sh
pico report --title "PICO bug: short summary" --body-file report.md
pico report --title "PICO bug: short summary" --body-file report.md --apply --plan-hash <preview-plan-hash>
```

Only the title and report file are sent. PICO never attaches the current
project's code, remotes, paths, logs, or run records. The report file must be
unchanged between preview and apply; `gh` must be installed and authenticated
for the apply step. The current directory does not choose the issue repository.

For later sessions in the same repo, the normal command is simply:

```sh
pico run .
```

### Watch a run in your browser

Open a second terminal in the target repository while `pico run .` is active:

```sh
pico watch .
```

Open the printed local URL (default `http://127.0.0.1:8765`). Pico Garden shows
the current and recent `/orchestrate` runs for that repository: tasks and routed
workers, spawn and worker activity, waves, elapsed time, concurrency, and
observed token metrics. The Garden view places the lead at the center, with
subagents and workers around it; select a node for its task, available
metrics, route explanation, ambiguity score, model capacity, and any recorded
five-dimension or work-demand scores. Routing details are compact metadata;
older runs show a clear unavailable message. The Conversation view shows
numbered parent directives, worker feedback, and parent decisions, with links
to the earlier step each one follows. Select a card's agent link to locate its
worker in Garden. These cards appear only when the parent records concise
`interaction` events; older runs show an empty-state explanation. The Timeline
view shows recorded events in order. Working is teal,
completed is green, waiting is gray, blocked is amber, and failed is coral.
Worker token figures are backend aggregates unless the provenance records an
individual total. When a run ends, it shows the final outcome, task
completion, a visual token report, and links to PRs recorded by linked
publication runs. It updates every two seconds and reads run provenance
without changing the active Pi session. Select an older run from the menu or
pause updates in the browser. Use `pico watch . --repo owner/repository` when
the checkout has multiple GitHub remotes, or `pico watch . --all` to see every
repository in the state directory. `--port <number>` changes the local port.

The viewer binds only to `127.0.0.1`. Its API exposes display metadata and
brief, explicitly recorded interaction summaries, not prompts, raw outputs,
or full trace files. Activity entries are recorded for runs
started with this version; older runs still show their saved tasks and metrics.
For checkout-local development, run `python3 tools/watch.py . --all
--state-dir ~/.local/state/pi-coding-orchestrator`.

To watch a whole workspace (see below), run `pico watch --workspace
workspace.yaml` or `pico watch --workspace <workspace-id>`. The Workspace view
shows one column per repository with each task's wave, declared readiness and
scheduler state, using the same states as `pico workspace schedule`
(completed, running, waiting for readiness, blocked by failure, refused,
needs reconciliation, and so on) and the same colours as the run view. Each
card shows what blocks it, its child run (linked to that run's own Garden
view under **Child runs**), blocker codes and PR links. A token report
totals input and output tokens per repository and for the workspace, and the
execution history lists recent scheduler sessions. With a manifest the view
also shows tasks that have not been launched yet; with an id alone it shows
the tasks recorded in the workspace record. It serves only ids, states,
codes, times and counters; it never serves task titles, goals, objectives,
prompts or logs, and it never opens execution records.

### Plan a mission across several repositories (read-only)

A workspace manifest (see `templates/workspace.yaml` and
`schemas/workspace.schema.json`) lists repositories with an explicit
`owner/repository`, a local checkout and a pinned commit SHA, plus tasks with
cross-repository dependencies. `pico plan workspace.yaml` validates the
manifest, resolves every checkout against its GitHub remotes, verifies the
pins and `gh` access, and prints execution waves. It spawns no worker and
performs no Git or GitHub mutation. An ambiguous checkout, an identity absent
from the remotes, two entries naming the same repository, a missing commit, a
dependency cycle or denied access all fail closed. Add `--json` for
machine-readable output or `--no-access-check` to plan offline (the plan then
records that access was not verified). Execution of such a plan is tracked in
issue #49; each repository keeps its own parent run and `target_repository`.
An implementation task may add a single-line `objective` (up to 4000
characters) with instructions scoped to that task's repository. The child
receives its own title and objective plus verified upstream contract metadata;
the workspace goal and other task titles are not sent to it. Older manifests
use the task title as the objective. After first dispatch the generated child
objective is immutable for that execution id: changing it requires a new task
id. The workspace record stores its SHA-256 digest, while the repository's
headless execution record retains the original text for restart and resume.
Executions created before this extension resume with their exact historical
objective when the manifest still reproduces it; changed instructions require
a new task id.

### Schedule a workspace across repositories

`pico workspace schedule workspace.yaml` launches every dispatchable task of
the manifest as a repository-scoped child run, concurrently, up to the
manifest's `max_parallel` (`--max-parallel` can only lower it) and never more
than one child per repository at a time. A child that fails, is cancelled or
is refused at dispatch blocks the tasks downstream of it; independent tasks
keep running. A finished child marks its task implemented, which does not
unblock dependents: once review or contract validation declares an upstream
task `ready_for_downstream` in the manifest, the next `schedule` run picks up
the tasks it unblocks. Restarting the scheduler never relaunches a recorded
task. Children outlive a scheduler crash and are observed again. An
interrupted child is reported as needing reconciliation and is resumed into
its existing run only with `--resume-interrupted`. Only one scheduler can run a
workspace at a time.

### Validate contracts and publish coordinated draft PRs

Tasks can declare the versioned contracts they `provide` and `require`, the
`checks` that validate them, and the `issue` their PR references; a task with
`kind: qualification` runs cross-repository checks instead of a child run.
Each task then moves forward only on evidence:

```sh
pico workspace validate workspace.yaml --task a1 --head pico/batch-1   # checks pass on that exact commit
pico workspace publish  workspace.yaml --task a1                       # dry run: review the plan_hash
pico workspace publish  workspace.yaml --task a1 --apply --plan-hash <hash>
pico workspace promote  workspace.yaml --task a1                       # CI observed green -> ready_for_downstream
```

Checks run in a temporary detached worktree, never in your checkout.
Publication uses the existing reviewed path: `.pi/autonomy.yaml` must allow
`push_branch` and `create_pr`, the push never forces, and the PR is a draft
whose body links the upstream PRs it depends on. A head that moved after
validation is refused. A downstream task dispatches only after its upstream is
promoted and every contract it requires is validated at the required version.
The scheduler runs qualification tasks as their own check wave once their
dependencies are validated. `pico workspace ci` observes CI and a merge;
nothing merges automatically.

### Launch one workspace task as a repository-scoped child run

`pico workspace launch workspace.yaml --task <id>` maps one task of a
workspace manifest to one headless execution bound to exactly that task's
repository. It re-runs the read-only plan first and refuses to launch when the
repository has a blocker, the checkout is not at the pinned commit, or the
task is still blocked by an upstream task that is not `ready_for_downstream`
or `merged`. The execution id is stable (`<workspace>--<task>`), so a relaunch
reconciles against the persisted execution instead of starting a duplicate
child, and `--resume` continues an interrupted child in the same parent run.
The child is an ordinary single-repository run with its own
`target_repository`; the launcher links its run id into the workspace record
in orchestrator state. `pico workspace status` reconciles every task and
`pico workspace cancel --task <id>` stops one child. Ordering and concurrency
across tasks belong to the scheduler (#54).

### Run one bounded orchestration headless

`pico exec start --repo owner/repository --execution-id <id> --objective "..."`
runs a single `/orchestrate` in Pi's non-interactive print mode with the
orchestrator extensions loaded, waits for it, and prints the execution record
with the parent run's outcome and trace path. The lifecycle guards, backend
preflight, and the target's `.pi/autonomy.yaml` apply exactly as in an
interactive session. Headless is not autonomous: when the policy grants any
mutating permission (worktrees, commit, push, PR, merge, deploy) the command
refuses unless `--allow-mutation` is passed. The execution id is an
idempotency key stored in orchestrator state, so repeating `start` never
launches a second process or run. A run that exits without an explicit outcome
is `BLOCKED`; a process that dies with the run still open is `interrupted` and
can be relaunched into the same parent run with `--resume`. `pico exec status`
reconciles the record against the live process and provenance, and
`pico exec cancel` stops the process and closes the run as `EXECUTION_CANCELLED`.

### Act from the Garden: validate, request PR, check CI, promote

`pico watch . --workspace workspace.yaml --controls` adds buttons to the
workspace view. A task whose child run finished can be validated against one
of its `pico/` branches. A validated task gets **Request PR**, and a task with
a PR gets **Check CI** and **Promote**. Every button first shows a preview: the
branch, commit and checks; or the publication plan and full PR body; or the
current CI state. The action runs only after you confirm, and only if nothing
changed since the preview. The launch URL carries a one-session token, so open
the Garden from that URL and keep it private. Without `--controls` the Garden
stays read-only.

The buttons call the same commands as the terminal, so the autonomy policy
still decides. When **Request PR** is refused because `.pi/autonomy.yaml` does
not allow `push_branch` and `create_pr`, you can either allow them for that one
reviewed PR (the policy file stays unchanged and the grant is recorded with the
publication), or switch them on in the permissions panel. Selecting a
permission there shows the exact diff of `.pi/autonomy.yaml` before writing
it; the change stays uncommitted until you commit it. `merge`, `deploy` and
`mark_pr_ready` can only be changed in the file.

A permission change stays uncommitted until you select **Commit policy** next
to the repository. It commits only `.pi/autonomy.yaml`, with your Git
identity, only while the checkout is on the repository's base branch (never on
a `pico/` work branch), and pushes nothing.

A manifest can set a `budget` (`max_tokens`, input plus output across all
child runs, and optionally `max_cost_usd`). Once it is reached the scheduler
launches nothing new and marks the remaining tasks **held by budget**; running
children continue. The Garden shows spend against the budget. Reported cost
comes only from Claude workers today, so the cost limit is a lower bound.

**Schedule now…** previews which tasks will launch, the parallel cap and each
repository's mutating permissions, and starts the scheduler in the background
once confirmed (with **allow mutation** and **resume interrupted** as explicit
options). A running child can be cancelled from its card. Selecting a model in
the models panel changes that tier to another model already used in the same
family, with its ambiguity capacity checked against the tier's floor, or
resets the tier to its default. Nothing in the Garden merges or marks a PR
ready.

### Publish an integrated batch

The parent orchestrator can now publish a reviewed `pico/` branch through
`tools/github_integration.py --publish`. It checks the target GitHub remote and
`.pi/autonomy.yaml` permissions, then produces a dry-run `plan_hash` covering
the branch commit, remote state, title, and PR body. Applying that hash pushes
one explicit branch ref without force and opens a draft PR. A stale plan or
changed PR body is rejected; a push followed by PR failure is recorded as a
partial publication for review and retry. This path requires both
`permissions.push_branch` and `permissions.create_pr` to be true. The
orchestrator prompt gives the full command sequence.

## Daily routine: update and restart pico (WSL)

Exit the running Pi session first, then from the target repository in WSL:

```sh
cd ~/projects/<your-repo>
git pull                        # update the target repo itself
pico sync .                     # refresh orchestrator + conservative setup + doctor
pico run . --repo owner/repository
```

- `pico run` already fetches the latest orchestrator commit on every launch.
  `pico sync .` adds the setup and readiness checks. If `.pi/orchestrator.lock` exists, the
  repo stays on the pinned commit. Use `pico update .` to advance the pin, then
  commit the lock.
- `--repo owner/repository` sets `PI_TARGET_REPOSITORY`, the GitHub repository
  that parent orchestration targets. It is only required when the checkout has
  several distinct GitHub remotes (for example `origin` = fork, `upstream` =
  main repo). With a single GitHub remote, `pico run .` is enough. The value must
  match one of the remotes; otherwise the run stops before doing anything.
- Pass extra Pi arguments after `--`, for example `pico run . --repo owner/repository -- --help`.
- If `pico doctor .` reports a dirty or broken cache, run `pico reset-cache --force`.
  Runtime state and provenance are kept.

## Launching Pi with the orchestrator

Do not start a target repository with plain `pi` if you expect orchestrator commands such as `/initialize`, `/audit`, or `/deliver`. Plain `pi` starts vanilla Pi and does not load this repository's extensions.

The canonical cached launcher is `pico run <target>`, a small installed wrapper around
`scripts/use-latest.sh run <target>` (which fetches and
resets the cached orchestrator to the latest `origin` default branch, or
`PI_CODING_ORCHESTRATOR_REF`, before every run and prints the commit in use)
and `scripts/run-local.sh <target>` (runs this checkout as-is and prints its
commit). Both pin the exact Claude executable in
`PI_ORCHESTRATOR_CLAUDE_BACKEND_IDENTITY` before Pi loads extensions.

A plain `pi -e ...` session has no pinned identity. Every route with a Claude
step then fails closed with `BACKEND_IDENTITY_UNINITIALIZED`, and the message
tells you to relaunch through one of the launchers. Use the manual commands
below only to troubleshoot extension loading, not for routed implementation work:

```sh
ORCH="$(scripts/use-latest.sh path)"
cd "$ORCH"
pi -e extensions/model-router.ts \
   -e extensions/workflow-commands.ts \
   -e extensions/mission-controller.ts \
   -e extensions/orchestration-lifecycle.ts
```

For extension troubleshooting, start with only the workflow command layer:

```sh
ORCH="$(scripts/use-latest.sh path)"
cd "$ORCH"
pi -e extensions/workflow-commands.ts
```

That session should expose `/initialize`, `/orchestrate`, `/audit`, and the other workflow commands. Add the remaining extensions incrementally if necessary to isolate a startup failure.

## How to use after Pi opens

### External GitHub backlog

The orchestrator can operate on Issues and labels in a target checkout without
adding Pi backlog files to that project:

```text
pi-coding-orchestrator
        ↓
worktrees/examplelab-feature
        ↓
Examplecorp/examplelab
```

In the commands below, set `ORCH_ROOT` to this orchestrator checkout and
`TARGET_WORKTREE` to the target checkout. The parent resolves `owner/repository`
from the target worktree's GitHub remotes. If they identify different
repositories, use `--repo owner/repository` to select one of them. An explicit
repository absent from all GitHub remotes fails closed; multiple identities
without an explicit selection are ambiguous. The parent can read issues and
labels and pass that state to analysis workers:

```sh
python3 "$ORCH_ROOT/tools/github_integration.py" --worktree "$TARGET_WORKTREE" --read
```

Workers return a JSON array of proposals, for example:

```json
[{"operation":"add_label","repository":"Examplecorp/examplelab","issue":401,"label":"semantic-contract","rationale":"Group contract work"}]
```

The parent saves those proposals to a file and runs the adapter first in its
default dry-run mode. The output includes the target, before and after states,
no-ops, conflicts, and a `plan_hash`. After reviewing authorization, the parent
repeats with `--apply --plan-hash <reviewed plan_hash>`; closing an issue or removing a label also requires
`--allow-destructive`. Supported writes are issue create/title-body update/comment/close/reopen,
PR title/body update and comment, plus label create/add/remove. Exact duplicate
comment bodies are treated as no-ops. PR merge/checkout/ready and arbitrary
`gh api` remain unavailable. The adapter uses the parent's
authenticated `gh` CLI (`gh auth login`), always passes `--repo`, and never
provides workers a generic GitHub command. Every batch outcome writes a record to the orchestrator's configured runtime
run store, including blocked and dry-run results.
Invalid or conflicting target identities stop before a GitHub mutation.

### Provenance ownership

Parent orchestration records and GitHub adapter records are owned by the
orchestrator installation that runs the tools. Cached `pico` launches store
mutable run records under `~/.local/state/pi-coding-orchestrator/runs`
(or `PI_CODING_ORCHESTRATOR_STATE_DIR/runs`), never in the target repository
and never in the cached Git checkout. Repository schemas and policies still
come from the exact orchestrator revision being executed. Each record names its
target in `parent_orchestration.target_repository`.

A target-local `.pi/provenance/runs` store is deliberately avoided: it would
dirty scientific project worktrees and increase the risk of accidentally
committing operational traces. Legacy untracked cached `provenance/runs/run-*.json`
records are migrated once into the state directory before an update. Direct
developer use of a local orchestrator checkout retains the historical
repository-local provenance layout unless `PI_ORCHESTRATOR_STATE_DIR` is set.

Mission state created by `/deliver` for a target is stored in
the state directory under `missions/<repository>-<hash>/missions.yaml`, never in the
target.
Linked worktrees share their repository's missions. A legacy target-local
`missions/missions.yaml` is copied there once and left in place.

No target-local `tools/backlog.py` or `backlog/backlog.yaml` is needed for this
workflow. Those files remain available for projects that opt into the local
backlog model.

For an unpublished local orchestrator checkout, launch Pi on the target
worktree through the local launcher. It loads every extension from
`$ORCH_ROOT` and pins the Claude backend identity:

```sh
"$ORCH_ROOT/scripts/run-local.sh" "$TARGET_WORKTREE" -- --prompt-template "$ORCH_ROOT/prompts"
```

When the target worktree has multiple distinct GitHub remotes, set
`PI_TARGET_REPOSITORY=owner/repository` to the parent-authorized target before
starting Pi, or use `scripts/use-latest.sh run "$TARGET_WORKTREE" --repo owner/repository`.
The selected repository must match one of those remotes.
The lifecycle extension resolves its Python tools, schema, routing assets,
and parent provenance under the installed orchestrator directory, independent
of the target worktree. Check the parent with `orchestration_record` action
`status` before target inspection; its diagnostic log includes the failing
operation and resolved orchestrator resource path if initialization fails.

Then run `/orchestrate Analyze and reorganize the existing GitHub backlog,
then implement the resulting issues.` The parent uses the read, dry-run, and
authorized apply commands above as needed.

The orchestration lifecycle extension starts a unified parent run before
routing. On each successful `route_task` result it checks the exact backend
tool, configured model, known minimum Claude Code version, and local Claude
authentication without an inference call. It blocks implementation worktree
creation until that preflight succeeds.

Preflight blockers:

| Code | Meaning | Reroute? |
| --- | --- | --- |
| `BACKEND_IDENTITY_UNINITIALIZED` | No valid launcher pin (`PI_ORCHESTRATOR_CLAUDE_BACKEND_IDENTITY` is missing or malformed). Relaunch through `scripts/use-latest.sh` or `scripts/run-local.sh`. | No |
| `MODEL_BACKEND_MISMATCH` | The resolved Claude executable differs from the identity pinned at launch. | No |
| `MODEL_BACKEND_UNAVAILABLE` | Required tool or executable missing. | Yes |
| `MODEL_AUTH_UNAVAILABLE` | Claude Code is not logged in. | Yes |
| `MODEL_BACKEND_VERSION_UNSUPPORTED` | Installed Claude Code is older than the routed model requires. | Yes |
| `MODEL_IDENTIFIER_UNSUPPORTED`, `MODEL_BACKEND_CONFIG_INVALID`, `ROUTING_RESULT_INVALID` | Configuration or route integrity failure. | No |

`route_task` preflights its route before returning when the lifecycle
extension is loaded. On a "Yes" blocker it excludes that route and re-selects
deterministically, recording `route_availability.excluded_routes`. A
replacement must be matched by the classification *after* project risk
floors, and it never drops Codex verification. For example,
`CODEX_CLAUDE_CODEX` may become `CODEX`, but `CLAUDE_CODEX` never becomes
`CLAUDE`. When no such candidate remains, the result is `NO_AVAILABLE_ROUTE`.
Every other blocker stops routing without a reroute. Individual GitHub adapter run IDs are
linked into the parent trace through `orchestration_record`; unreported terminal
outcomes finalize as BLOCKED. Known model/version requirements live in
`routing/backend-compatibility.json`. That file also maps canonical Claude
policy models to Claude Code model IDs at the `ClaudeWorker` invocation boundary;
provenance retains the canonical model. Model-specific account entitlement is
only confirmed by the eventual backend invocation.

If your Pi setup has `/orchestrate` configured, use it with a concrete
engineering request:

```text
/orchestrate Audit this repo for security and correctness issues, then propose prioritized fixes.
/orchestrate Fix the metadata persistence bug and add regression tests.
```

If `/orchestrate` is not configured, print the prompt and paste/use it in Pi
with your request:

```sh
pico prompt
```

When running Pi through the helper, the loaded workflow commands are machine-checked against this registry:

<!-- workflow-command-registry --> /initialize /lookup /orchestrate /audit /audit-backend /audit-frontend /quick-audit /plan-implementation /update-agents /orchestrate-auto

### Choose a command

| Command | Use it for | Example |
| --- | --- | --- |
| `/initialize` | Create missing project-local configuration conservatively. | `/initialize` |
| `/lookup <question>` | Answer one bounded, read-only repository question directly in Pi. It does not start an orchestration run, so it does not produce a parent trace. | `/lookup Which file defines the published guide URL?` |
| `/orchestrate <objective>` | Carry out a concrete engineering task through routing, worker execution, validation, and a trace. | `/orchestrate Fix the metadata save race and add a regression test.` |
| `/quick-audit [scope]` | Run a bounded audit focused on reproducible correctness and security defects. | `/quick-audit Check the authentication middleware for bypasses.` |
| `/audit [scope]` | Audit the repository broadly for correctness, security, reliability, maintainability, and test gaps. | `/audit Review the import pipeline and its tests.` |
| `/audit-backend [scope]` | Focus an audit on APIs, persistence, concurrency, and backend tests. | `/audit-backend Check the metadata write path.` |
| `/audit-frontend [scope]` | Focus an audit on UI state, accessibility, UX regressions, and frontend tests. | `/audit-frontend Check the metadata editor.` |
| `/plan-implementation <objective>` | Inspect the code and produce an implementation plan. It does not implement unless you explicitly ask. | `/plan-implementation Add bulk metadata editing with undo.` |
| `/update-agents [scope]` | Improve `AGENTS.md` using verified repository facts. | `/update-agents Document the test and publishing commands.` |
| `/orchestrate-auto <objective>` | Work through the orchestration protocol within configured authority, stopping at approval boundaries. | `/orchestrate-auto Fix the failing metadata tests and open a PR.` |

All commands in the table except `/initialize` and `/lookup` use the shared
orchestration protocol. `/audit`, `/audit-backend`, `/audit-frontend`,
`/quick-audit`, and `/update-agents` have default objectives when called without
arguments. `/lookup`, `/orchestrate`, `/plan-implementation`, and
`/orchestrate-auto` require an argument. Command failures appear as Pi
notifications.

For work that spans several engineering tasks, use `/deliver <goal>` to create
a durable mission and queue planning for its next wave. The mission commands
manage mission state; they do not merge PRs or deploy changes:

| Command | Use it for |
| --- | --- |
| `/deliver <goal>` | Create a mission and start planning. |
| `/deliver-status [MISSION-ID]` | Show one mission, or list all missions without an ID. |
| `/deliver-link MISSION-ID BACKLOG-ID` | Link an existing backlog item to a mission. |
| `/deliver-pause MISSION-ID` | Pause a running mission. |
| `/deliver-resume MISSION-ID` | Resume a planned or paused mission. |
| `/deliver-cancel MISSION-ID` | Cancel a mission after interactive confirmation. |

### Tips for effective requests

- Start with `/lookup` for a narrow fact or location question. Use `/orchestrate`
  when you want changes, a trace, or a routed review. A focused `/lookup` avoids
  the setup and worker calls of an orchestration run.
- Name the outcome and the affected area. Include the behavior that must be
  preserved, an acceptance check, and whether you want a plan, code changes,
  or a PR. For example: `/orchestrate Fix duplicate metadata saves in
  src/metadata; preserve existing API responses, add a regression test, and
  open a PR.`
- Give audits a boundary such as a feature, directory, or risk. Use
  `/quick-audit` for a fast, narrow pass and `/audit` when you need wider
  coverage. State whether you want findings only or fixes too.
- Use `/plan-implementation` when you want to review an approach before code
  changes. Use `/orchestrate-auto` only when the repository's configured
  authority matches the actions you want it to take.
- After a routed run, inspect its trace and compare usage from the orchestrator
  checkout. With the cached launcher, set `PI_ORCHESTRATOR_STATE_DIR` to the
  directory printed by `pico state-path`, then run
  `python3 tools/backlog.py trace --latest` or
  `python3 tools/backlog.py usage --json`.

## Useful helper commands

From any target repository:

```sh
pico update .      # refresh code; advances .pi/orchestrator.lock when present
pico path          # print the cached code checkout
pico state-path    # print persistent runtime-state directory
pico setup .       # conservative target-repository initialization
pico setup . --github-templates
pico doctor .      # human-readable readiness report
pico doctor . --json
pico sync .        # update/setup/readiness smoke test
pico pin .         # pin the current resolved commit into .pi/orchestrator.lock
pico unpin .
pico reset-cache --force  # discard/reclone cached code, preserve runtime state
pico models show  # effective model profiles (also: validate|set|reset)
pico prompt
pico run . --repo owner/repository  # select target when several GitHub remotes exist
pico run . -- --help
```

For a reproducible project, commit `.pi/orchestrator.lock`. `pico run .`
then uses that exact commit unless `PI_CODING_ORCHESTRATOR_REF` explicitly
overrides it. To advance the project deliberately:

```sh
pico update .
git diff -- .pi/orchestrator.lock
```

You can also temporarily select a reviewed release, branch, or commit:

```sh
export PI_CODING_ORCHESTRATOR_REF=v1.0.0
pico run existing-project
```

If this repository is already cloned locally, the same helper is available as
`scripts/use-latest.sh`. You can use it directly or install `pico` from that checkout:

```sh
pi-coding-orchestrator/scripts/use-latest.sh install
pico run existing-project
```

The helper refuses to update a dirty cache or one whose Git origin differs from
the configured orchestrator URL.

## Recovery, uninstall, and platform support

If the cached code checkout becomes dirty or corrupted, inspect it first with
`git -C "$(pico path)" status`. To discard and reclone only the cached code
while preserving runtime provenance/state:

```sh
pico reset-cache --force
pico doctor .
```

Uninstall the launcher without deleting cache, state, or model configuration:

```sh
pico uninstall
```

Optional destructive cleanup is explicit:

```sh
pico uninstall --purge-cache
pico uninstall --purge-cache --purge-state --purge-config
```

The supported launcher environment for v1.0 is POSIX shell on Linux and macOS;
Windows users should use WSL2 (recommended) or a compatible Git-Bash
environment. Native PowerShell is not a supported launcher surface in v1.0.
CI executes the full suite on Ubuntu and macOS.

## What it does

At a high level, the orchestrator helps Pi:

- classify a coding/audit request;
- call `route_task` to choose a deterministic execution route;
- run the returned execution plan with the appropriate Claude/Codex family;
- require independent review when policy says it is needed;
- keep project-local constraints additive through `.pi/routing.yaml`;
- optionally record a user-facing provenance trace for the run.

The mission controller is a separate, goal-level layer. `/deliver <goal>`
creates durable mission state in `missions/missions.yaml` and queues bounded
`/orchestrate` planning for the next engineering task. Mission goals that
contain explicit bullets like `- ORCH-016: Scheduler invoking /orchestrate` can
be expanded with `python3 tools/mission.py plan-backlog MISSION-001` into a
validated backlog DAG linked back to the mission. `/deliver-status`,
`/deliver-link`, `/deliver-pause`, `/deliver-resume`, and `/deliver-cancel`
are thin command wrappers around `tools/mission.py`; they do not push branches, mutate GitHub,
mark PRs ready, merge, deploy, or replace the `/orchestrate` execution engine.
Mission completion is reconciled only from referenced backlog items that are
already deterministically `completed`.

The four top-level routes are:

- `CODEX`
- `CLAUDE`
- `CLAUDE_CODEX`
- `CODEX_CLAUDE_CODEX`

`route_task` is the source of truth for route selection. Do not choose a route
manually based on language, framework, or preference.

## Worker isolation, effort routing, and context management

These are enforced by the lifecycle extension during a parent `/orchestrate`
run. None of them depends on the model following prompt instructions.

**Repository-scoped worker sessions.** A worker session belongs to exactly
one repository, parent run, phase, and step. State moves between workers as
explicit artifacts: specifications, reviews, test results, the git diff, and
checkpoints. Backend conversation history is never reused.

| Blocker | Cause |
| --- | --- |
| `SESSION_ISOLATION_REQUIRED` | `AskClaude` called with `isolated: false`. When `isolated` is omitted, it is forced on. |
| `SESSION_REPOSITORY_MISMATCH` | The Claude worker directory does not share the target's git common directory. Linked worktrees of the target are allowed. |
| `SESSION_SCOPE_VIOLATION` | `message_subagent` tried to continue a subagent that was not spawned in the current parent run. |
| `SESSION_SCOPE_UNVERIFIED` | Between parent runs, `message_subagent` tried to continue a subagent that was not spawned in this Pi process. |
| `WORKER_WORKSPACE_UNBOUND` | A mutating Claude or Codex step has no worktree bound via `orchestration_record` `bind_workspace`. |
| `WORKER_WORKSPACE_UNSTATED` | A mutating Codex worker's `task` does not name its bound worktree path. |
| `CLAUDE_WORKER_REQUIRED` | A mutating Claude step was called through `AskClaude`; it runs through `ClaudeWorker` in its bound worktree. |
| `WORKER_TASK_UNATTRIBUTED` / `WORKER_TASK_UNROUTED` / `WORKER_STEP_UNKNOWN` | Several tasks are routed and the worker call names none, names an unrouted task, or names a step outside its plan. |
| `WAVE_UNDECLARED` | Workers of different tasks would overlap outside one declared wave. |
| `INTEGRATION_IN_PROGRESS` | A worker launch while the parent integrates a wave. |
| `WORKER_SCOPE_VIOLATION` | The parent checkout changed during a worker call. Further workers are blocked; nothing is reverted. |
| `MUTATION_MODE_MISMATCH` | The `AskClaude` `mode` differs from the routed step's mutation permission. |
| `WORKER_TIMEOUT` / `WORKER_ABORTED` / `WORKER_OUTPUT_LIMIT` | A `ClaudeWorker` run exceeded `PI_CLAUDE_WORKER_TIMEOUT_MS` (default 30 min), was aborted, or exceeded its output cap; its Claude Code process is killed. |

**Claude workers.** Routed Claude steps run through `ClaudeWorker`, a
lifecycle tool that starts the preflight-pinned Claude Code executable as a
headless subprocess (`claude -p --output-format json
--no-session-persistence`). Its only inputs are the prompt, which starts with
`[pico-task <task_id>:<step_id>]`, and optionally the routed `model` and
`thinking`. The lifecycle derives the working directory (the step's bound
worktree; a read-only step without one uses the target checkout), the Claude
Code model, and `--effort` from the route. A mutating step gets
`--permission-mode acceptEdits` with Read, Edit, Write, Glob, Grep, Bash, and
Agent. A read-only step gets only Read, Glob, and Grep. `git push`, `gh`,
`git worktree`, and branch deletion are denied tools; other out-of-scope
actions rely on the worker contract and the parent-checkout snapshot.
Because every call has its own process directory, mutating Claude workers of
one wave run concurrently. `AskClaude` remains accepted for read-only routed
steps and outside parent runs. Its isolation and the repository check also
apply in follow-up turns after a parent run, and `pico run` sets the bridge's
`askClaude.defaultIsolated` to `true`. Every worker call is checked against a
before/after snapshot of the parent checkout.

**Parallel waves.** Every successful `route_task` creates an approval for that
task only, so several routed tasks keep independent approvals, preflights, and
bound worktrees (keyed by task and step). Worker calls name their task and step:
`spawn_subagent` with `label: "<task_id>:<step_id>"`, `ClaudeWorker` (and
`AskClaude`) with a prompt starting `[pico-task <task_id>:<step_id>]`;
`message_subagent` inherits the spawn's attribution. With exactly one routed
task an unambiguous untagged `spawn_subagent` or `AskClaude` call still
resolves to it; `ClaudeWorker` always requires the tag. The parent declares a wave of routed tasks with
`orchestration_record` `declare_wave` (rejected with `WAVE_SCOPE_OVERLAP`,
`WAVE_LIMIT_EXCEEDED` above `.pi/autonomy.yaml` `limits.parallel_tasks`, or
`WAVE_TASK_UNROUTED`), launches their workers in one tool batch, then brackets
integration into a local `pico/batch-<n>` branch with `integrate_begin` (refused
with `INTEGRATION_WORKERS_IN_FLIGHT` while workers run) and `integrate_end`,
which re-bases the parent-checkout snapshot. Codex and Claude workers of a wave
mutate in parallel, each in its own worktree. With
`pr_granularity: batch` (default) each integrated batch gets one push and one
PR; CI never blocks the next wave, and `limits.open_prs` (default 2) caps open
batch PRs by stacking new batches on the newest batch branch.

**Effort routing.** Policy v7 assigns capacity and reasoning effort in five
tiers, `light < economy < standard < high < max`. Task ambiguity is scored 0–10
after inspecting repository context, then matched to each configured worker
tier’s declared ambiguity capacity; see [the ambiguity rubric](docs/ambiguity.md).
The captain also records [work-mode demand](docs/work-modes.md) for search,
analysis, writing, implementation, verification, and coordination when
decomposing tasks. Graduated rules lower both for bounded or low-risk work
(for example `ops-light` assigns
`assigned_reasoning_effort: low`), and assignment never raises effort.
`ClaudeWorker`, `AskClaude`, and `spawn_subagent` receive the routed
effort as `thinking` and the routed model as `model`; any other value is
blocked with `REASONING_EFFORT_MISMATCH` or `MODEL_BACKEND_MISMATCH`. Escalation is a
re-classification through `route_task`, and the phase records a closed
`escalation_reason` (`FAILED_ATTEMPTS`, `REVIEW_FINDINGS`, `AMBIGUITY`,
`ARCHITECTURE`, `SECURITY_SENSITIVE`, `SEMANTIC_RISK`, or `CONTEXT_BUDGET`).

**Parent context management.** Pi owns automatic compaction, including its
trigger, summary, and retained conversation window. The orchestrator never
calls `ctx.compact()` or blocks worker launches at a custom token limit. Pi
may still compact under its own settings; those events are recorded as Pi
compactions. The parent run stays active across compaction and ordinary turn
boundaries. `routing/context-policy.yaml` contains only an optional structured
checkpoint output size. Explicit checkpoints remain available through
`tools/context_budget.py checkpoint`; they are validated against
`schemas/context-checkpoint.schema.json`, secret-scanned, and stored under
`checkpoints/<owner>__<repo>/<run_id>/<seq>.json`. Checkpoint failures do not
block orchestration. Claude Code and Pi subagent workers manage their own
context and start fresh.

Parent provenance (`parent_orchestration.context`) records the baseline, peak
context and working tokens, Pi compactions, explicit checkpoint references, and per-backend
worker usage (invocations, duration, and input/output/cached tokens), the peak
number of concurrent workers, and the time with at least one worker in flight.
Pi assistant usage and successful subagent spawns are also recorded. Individual
lead model calls record turn index, duration, tokens, phase, and classified task type when attribution
is unambiguous. Use `python3 tools/backlog.py usage --json` to compare raw
observed usage across runs; missing provider counters are counted separately.
Finishing
`/orchestrate` returns a short stats line with wall time, spawned subagents,
worker calls, observed models, and token counts. A total token count appears
only when every participating backend reports an explicit total; missing
provider usage remains `unknown`.

`parent_orchestration.waves` records each wave's tasks and integration state, and
each phase its `wave_id`. From these the run's `orchestration_metrics` derive
`delegation_topology` and `parallel_speedup` (serial estimate over wall clock).
Only values observed in Pi messages or tool results are recorded; anything
unobserved stays null.

## Project-local setup

`init .` creates conservative starter files in the target repo:

- `AGENTS.md` — repo-specific instructions for agents;
- `.pi/routing.yaml` — additive constraints and risk floors only;
- `.pi/autonomy.yaml` — execution-authority defaults for mission-level delivery;
- `.pi/orchestrator-pr-workflow.md` — branch, PR, trace, and issue workflow.

`.pi/routing.yaml` can add constraints, but it cannot replace global routes,
models, permissions, review requirements, or fallback behavior.

For simple use, keep this orchestrator cached and load it from the target repo.
Commit `.pi/orchestrator.lock` when a scientific software project needs a
reviewed, reproducible orchestrator revision. For a deliberately vendored
orchestrator, review the full copied tree as project-owned software.
`pico setup . --copy-extension` copies the complete runtime into `.pi/`: all
extensions including the lifecycle, their tools, schemas, policies, templates,
and a launcher. Start it with `.pi/scripts/run-local.sh .`. Its provenance
goes to the user state directory, not the target.

## Validate this repository

When working on this orchestrator itself, run:

```sh
python3 tools/validate_routing.py
python3 tools/backlog.py validate
npm test
npm run typecheck
python3 -m pytest
```

For a minimal Python-only check if `pytest` is unavailable:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

## Traces and release workflow

Runs can be summarized with:

```sh
python3 tools/backlog.py trace --latest
python3 tools/backlog.py trace --backlog-id ORCH-011 --latest
python3 tools/backlog.py usage --json
```

A release checklist is maintained in [`docs/release-checklist.md`](docs/release-checklist.md).

Known limitations:

- worker token telemetry depends on backend/provider support (`ClaudeWorker`
  and Pi subagents report token usage; `AskClaude` reports duration only);
- mission and phase token budgets are planned (ORCH-025) and not yet enforced;
- cost estimation is intentionally absent without a reviewed pricing table;
  only Claude Code's own `total_cost_usd` is recorded, as
  `estimated_cost_usd` in `worker_usage`;
- merge and deploy actions are not autonomous.

## Technical reference

The previous long-form README has been moved to
[`docs/technical-reference.md`](docs/technical-reference.md). It contains the
full policy architecture, schemas, routing semantics, execution-plan details,
provenance model, GitHub sync behavior, and historical examples.

Important source files:

| File | Purpose |
|---|---|
| `prompts/orchestrate-core.md` | Short initial orchestration prompt loaded by workflow commands. |
| `prompts/orchestrate.md` | Full operational reference read in relevant sections during execution. |
| `extensions/model-router.ts` | Pi extension that exposes `route_task`. |
| `extensions/workflow-commands.ts` | Pi extension exposing `/initialize`, `/orchestrate`, audit, and planning commands. |
| `extensions/mission-controller.ts` | Pi extension that exposes `/deliver*` mission commands. |
| `tools/mission.py` / `tools/mission_store.py` | Durable mission state and reconciliation CLI. |
| `tools/route_task.py` | JSON-over-stdio runtime adapter. |
| `tools/routing_engine.py` | Shared deterministic routing evaluator. |
| `routing/default-policy.yaml` | Route predicates, precedence, fallback, execution plans, model profiles. |
| `routing/delegation-policy.yaml` | Nested delegation policy inside routed steps. |
| `schemas/task.schema.json` | Required task classification shape and classification rubric (field descriptions). |
| `templates/project-routing.yaml` | Starter target-project constraints file. |
| `scripts/use-latest.sh` | Fetch/update/run helper for existing projects. |

## Policy contract anchors

This short section keeps the README aligned with the deterministic validator.
For full details, see [`docs/technical-reference.md`](docs/technical-reference.md).

Successful `route_task` results include `capacity_assignments` with these
fields: `step_id`, `authored_capacity_tier`, `assigned_capacity_tier`,
`authored_reasoning_effort`, `assigned_reasoning_effort`, `rule`,
`decision_reason`, `guard_predicates_passed`, `blocking_predicates`, and
`selected_model_profile`. Capacity tiers are ordered
`light < economy < standard < high < max`; execution plans author only
`standard`/`high` (`max` is authorable but authored nowhere by default), and
post-route assignment never raises reasoning effort (policy v7).

Task classification follows the rubric in `schemas/task.schema.json`: every
risk flag defaults to its low-risk value, and AGENTS.md guardrails govern
worker conduct, not task risk. An escalating value (`failure_cost=high`,
`semantic_correctness=critical`, `ambiguity>=7`,
`behavior_preservation=required`, `assumptions_must_be_explicit`,
`security_sensitive`, `destructive_operation`) needs a one-line
`escalation_evidence` entry; otherwise `route_task` returns `ok: false` with
`blocker_code: CLASSIFICATION_EVIDENCE_MISSING`. Values raised only by project
risk floors are exempt. Operational work uses the task types `vcs_operation`,
`issue_management`, `config_edit` and `ci_fix`.

Capacity assignment rules are ordered; per step, the first rule whose
`step_match` selects the step and whose guard fully holds wins:

| Rule | Direction | Selects | Guard (all must hold) | Assigns |
|---|---|---|---|---|
| `ops-light` | down | worker, authored standard/high | `task_type` in vcs_operation/issue_management/config_edit/documentation/ci_fix, `failure_cost` low/medium, small scope, `ambiguity<=6`, `semantic_correctness` not critical, no diagnosis | `light`, effort low |
| `low-risk-worker` | down | worker, authored standard | the v5 low-risk guard (low cost/ambiguity, small, not cross-layer, no production feature, ...) | `economy`, effort low |
| `bounded-implementation-worker` | down | worker, authored high | small/medium scope, `ambiguity<=6`, `failure_cost` not high | `standard`, effort medium |
| `bounded-specifier` | down | specifier, authored high | small scope, `ambiguity<=3`, `failure_cost` not high | `standard`, effort medium |
| `critical-escalation` | up | specifier and reviewer, authored high | exactly `failure_cost=high`, `semantic_correctness=critical`, `ambiguity>=7` | `max`, effort unchanged |

Every down rule also requires `security_sensitive == false` and
`destructive_operation == false` (validated centrally), targets only
non-independent-review worker/specifier steps of analysis/implementation
intent, and only lowers the tier. Independent-review steps are never lowered;
only `critical-escalation` may raise a step, and only to `max`. Review-intent
workers are never lowered. Each predicate entry in `guard_predicates_passed`
and `blocking_predicates` records `rule`, `field`, `operator`, `value` and
`actual`; `selected_model_profile` records `family`, `capacity_tier`, `model`,
and `context_window_tier`.

Nested delegation is governed by `routing/delegation-policy.yaml`: prefer the
smallest useful fan-out, delegate only when independent bounded work or a distinct
specialist contract provides clear utility, and keep synthesis, integration, and
the final reply with the lead. Small dependent tasks, shared writes, a fixed
execution graph, or one slow external operation favor direct execution. Parallel
mutation requires a dedicated worktree
and branch created from a pinned commit for each mutating worker; read-only
review does not need that mutation worktree protocol.

This policy adapts the decision guidance in the official OpenAI Docs, as
mapped in [the orchestration guidance comparison](docs/orchestration-guidance.md).
The API beta flags, SDK handoff methods, and native Codex subagent configuration
are specific to those runtimes; Pi continues to enforce its own route, worker
model profiles, concurrency limits, and mutation boundaries.

# Claude Code backend selection

`scripts/use-latest.sh run` and `scripts/run-local.sh` resolve the installed
`claude` executable before Pi starts and set the Claude bridge's supported
`provider.pathToClaudeCodeExecutable` setting in the Pi agent configuration.
They preserve other bridge settings. This makes the bridge use the same
executable that routing preflight validates. A project `.pi/claude-bridge.json`
override to a different executable blocks launch. Preflight and the lifecycle
recheck the resolved executable and version before creating a worktree and
before every routed Claude call. `ClaudeWorker` runs the preflighted file
itself (the resolved `backend_executable`), never a fresh `PATH` lookup.
