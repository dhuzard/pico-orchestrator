# Pico GitHub Issues dispatcher (opt-in prototype)

## Bounded human-authorized repairs

When a delivery has failed checks or a current human change request, create a
fresh queue issue describing the exact repair scope and acceptance checks. Approve
it with the normal interactive fingerprint command, adding `--repair-of <original
queue issue> --allow-mutation`. This binds it to the re-observed PR head, stores
lineage, and consumes one repair allowance for that root delivery. The allowance
is durable across restart, stale issues and reapproval; it is not reset by a new
queue number. The original execution record is unchanged. A changed PR head or
resolved failure invalidates the queued repair before launch. Current duration
and call caps apply; there is no automatic repair from a comment or label and no
automatic merge. The fingerprint authorizes the exact new repair task; the
dispatcher does not infer a code scope from a reviewer comment.

**Status:** prototype; does not install, enable, or run anything by default. No Moltis integration yet. Only one active job globally. Queued issue contents are untrusted input. This adapter is NOT a security sandbox for the lead or Codex workers, and is not a substitute for `.pi/autonomy.yaml`.

## Why this lives in Pico

`tools/issue_dispatcher.py` is an intake adapter calling the **existing** `pico exec start|status` headless lifecycle. It does not implement a second agent/router/scheduler, and has no dependency on Moltis. Moltis can later create issues in the queue through a scoped MCP tool.

## Queue repository

Create a *private* GitHub repository, for example `dhuzard/pico-tasks` (not created by this PR). Enable Issues. Create the `pico:queued` label. Every task uses the following exact Markdown structure:

```markdown
## Target repository
dhuzard/oratlas

## Objective
Implement validation for missing provenance identifiers. Work in the target repository only.

## Acceptance criteria
Relevant unit tests pass. Report changed files and any unmet checks.
```

Add the `pico:queued` label. The target must be on the local allowlist. The issue's title/body fingerprint is recorded during local human approval; changing either before execution blocks dispatch. A GitHub label or comment *alone* cannot authorize execution. Do not include secrets in issues. Private repositories limit visibility, not code-execution authority.

## Configure the MSI WSL worker

Clone the reviewed Pico commit into `/home/dhuzard/src/pico-orchestrator` (or adapt paths). Ensure that `pico`, `gh`, `pi`, and the desired Claude/Codex backends run for the same service user. Use separate credentials for the private queue and permitted target repositories. Restrict each credential to the repository and operations it needs; never place tokens in an issue.

Create `/home/dhuzard/.config/pi-coding-orchestrator/dispatcher.json` (mode 0600):

```json
{
  "queue_repository": "dhuzard/pico-tasks",
  "targets": {
    "dhuzard/oratlas": "/home/dhuzard/projects/oratlas",
    "dhuzard/diggypaper": "/home/dhuzard/projects/diggypaper"
  },
  "poll_seconds": 60,
  "max_duration_seconds": 1800,
  "max_lead_calls": 30,
  "state_path": "/home/dhuzard/.local/state/pi-coding-orchestrator/issue-dispatcher.sqlite3",
  "queue_token_file": "/home/dhuzard/.config/pi-coding-orchestrator/queue-token",
  "target_gh_config_dir": "/home/dhuzard/.config/pi-coding-orchestrator/target-gh",
  "gh_executable": "gh",
  "pico_executable": "pico"
}
```

Set up `queue_token_file` as a mode 0600 file containing only a queue-scoped token. Set up the separate target directory with a target-scoped GitHub CLI login using `GH_CONFIG_DIR=/absolute/path/to/target-gh gh auth login --hostname github.com --insecure-storage`. This explicitly stores the target credential in that directory's `hosts.yml`, which must be mode 0600 inside a mode 0700 directory, both owned by the service user. Verify the active GitHub account and its repository permissions without printing tokens. A system keychain alone is not accepted: shared keychains could silently select the wrong account. Both settings are required together, and the queue token file must be outside the target directory. Missing credentials, insecure permissions, failed target authentication, or inability to read the approved target repository block a new launch. The repository preflight verifies read access and identity, not permission to push or create a pull request. Omitting both settings preserves the legacy shared-auth behavior; use both for separate accounts.

Queue issue reads and comments receive the queue token only in their `gh` process environment. The Pico process receives the target `GH_CONFIG_DIR` and no inherited GitHub token environment variables; target `gh` operations use that configuration. Git transport authentication must independently be configured for the target identity and remote. Neither configuration grants publication: the target autonomy policy and any applicable operator approval still govern push and PR operations.

Before approving a mutating task, configure the intended Git author and committer
identity for the service account. `pico exec start` now refuses a new mutating
launch or resume with `GIT_IDENTITY_INVALID` before starting Pi if either identity
is missing or malformed. It resolves each name/email from its explicit
`GIT_AUTHOR_*` or `GIT_COMMITTER_*` environment value, then Git's `author.*` or
`committer.*` config, then `user.name` / `user.email`. It never infers identity
from commit history, the OS account, hostname or the fallback `EMAIL` variable.
An explicit empty or malformed value is refused, not replaced by a fallback.
Names may contain spaces; names/emails must not contain control characters or
angle brackets, and emails must contain a nonempty local part and domain.

The checked values are pinned in the lead's child environment. Claude workers
receive exactly these four Git identity variables through the existing allowlist;
Git credential helpers, SSH agents and arbitrary `GIT_*` overrides remain withheld.
This validates setup, not the identity's ownership or model containment. No Git
configuration is written and no commit is made by preflight. Read-only launches
and already-running/finished execution lookups do not require a Git identity.

This is credential routing, not process isolation. The queue token path is passed to Pico so its Claude worker sandbox can deny reads of that file. The target checkout must run a Pico revision that includes this dispatcher credential-scoping change: `pico exec` honors the target `.pi/orchestrator.lock`, and an older pinned runtime will ignore the path and will not provide this additional denial. Verify the effective revision before running; placing the token under the standard `~/.config/gh` credential directory also uses the existing worker deny rule. The Pi lead runs as the service user and can read the target config, and same-user processes may be able to read the queue token file. The lead can use its target authority, and unrestricted lead or Codex processes can reach same-user files; worker credential filtering does not protect against an unsandboxed lead. Restrict both credentials to their respective repositories, use a dedicated service account or stronger isolation where needed, and do not claim the queue credential is protected from a malicious lead.

The allowlist paths must be real local Git checkouts, with matching GitHub remotes and a valid committed `.pi/autonomy.yaml` according to Pico's existing policy. This prototype does not create, clone, synchronize, repin or clean any target. Explicitly check repository dirtiness and the effective policy before enabling mutation. The `pico exec` preflight and autonomy policy are still authoritative. Because unattended Pi leads and Codex workers are not fully OS-contained, use an isolated service environment and restricted credentials for sensitive repositories.

## Approval and test

```bash
python3 tools/issue_dispatcher.py --config ~/.config/pi-coding-orchestrator/dispatcher.json approve --issue 1
# Optional, ONLY after inspecting the policy and committing the intended local mutation permissions:
python3 tools/issue_dispatcher.py --config ~/.config/pi-coding-orchestrator/dispatcher.json approve --issue 2 --allow-mutation
python3 tools/issue_dispatcher.py --config ~/.config/pi-coding-orchestrator/dispatcher.json once
python3 tools/issue_dispatcher.py --config ~/.config/pi-coding-orchestrator/dispatcher.json status
python3 -m unittest discover -s tests -p test_issue_dispatcher.py -v
```

`approve` requires a local interactive terminal and typing the exact task fingerprint prefix. The service itself never self-approves. `--allow-mutation` permits mutations already authorized by the target's autonomy policy; it is *not* limited to code edits by this adapter. **Keep push, PR, merge and deploy disabled in the target policy unless separately reviewing an expanded authority scope.** Do not expose the approval CLI through Moltis or Telegram.

## Independently supervised systemd service (not enabled)

Only after review, copy `deploy/systemd/pico-dispatcher.service` to `/etc/systemd/system/pico-dispatcher.service`, verify user, paths and installed binaries, then explicitly run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pico-dispatcher.service
sudo systemctl status pico-dispatcher.service
journalctl -u pico-dispatcher.service -f
```

The unit has **no dependency on `moltis.service`**. It runs as `dhuzard`, so processes share that account's credentials and filesystem; process independence is not privilege isolation. The service and launched workers depend on MSI + WSL uptime. On service restart, systemd's default control-group termination may interrupt children; the dispatcher records `interrupted` rather than auto-resuming or starting a fresh execution. Recovery requires a separate inspected operator action.

## Current restrictions / follow-ups

- A private queue repository is an optional separate project; this PR neither creates it nor changes target projects.
- No automatic issue discovery beyond operator approval (`approve --issue N`). Approved jobs are stored in SQLite and polled. Only one runs at a time; it does not yet implement #34 dependencies or merge-gating.
- No automatic draft PR, merge, deployment, permission escalation, task creation or autostart on installation.
- Status is written as GitHub issue comments when possible. Comment delivery is retried on failure, but a crash between comment and local checkpoint can duplicate a status comment.
- Task authorization is a local exact-title/body digest, not a cryptographic statement about human identity against a compromised same-user process. Store state and credentials with strict permissions. Any lead capable of reading the local state or GitHub credentials remains part of the threat model.
- `interrupted` is terminal for this prototype; no unattended retries, no resume, no automatic expansion of permissions. A new issue and new explicit approval is required for a new execution attempt.
- `gh issue list` is not used for authorization; non-operator comments, labels and issue contents never grant execution.
- Integrate #34, #63 and #87 before calling this a production unattended coding system. Add a real WSL/Windows sign-in and service restart pilot, with mock-agent tests first.

## Delivery observation and human handoff

Completed, partial, blocked and interrupted executions are reconciled on every service poll, including after restart. The execution outcome remains unchanged; a separate SQLite delivery projection records the observed publication, PR head, CI, human review, attention reason and next action. `status` includes this projection in each task's `delivery` field. The observer only reads GitHub and local provenance; it never starts a model, repairs code, approves, merges or deploys. Repairs require a separate explicitly authorized execution; no comment grants authority.

Configure expected check names for each target, for example:

```json
"delivery_expected_checks": {"dhuzard/oratlas": ["build", "test"]}
```

A nonempty list is required to establish passing expected checks. It is an operator-maintained expectation, not a replacement for GitHub branch rules. The observer also reads branch protection and includes any accessible required contexts; inaccessible protection or rulesets leave merge readiness unknown. Passing expected checks invites a person to review the PR; it does not mean the PR is mergeable or satisfy unknown GitHub rules. Missing, skipped, neutral, cancelled or failed checks never count as passed. A changed head during polling invalidates the observation. Only reviews by GitHub `User` accounts at the observed head count as human decisions; a dismissed or older approval does not count as current.

An applied PR creation or observed publication receipt must appear in a linked publication child for the approved target. The PR's base and head repositories must both match the target, and its current head must match integrated evidence in parent provenance. New publication receipts retain the exact commit, branch, parent run and operation stage before remote writes, allowing the final head to be identified across multiple waves. Legacy records without a receipt require one unique integrated head. Later commits, missing child provenance and cross-repository PRs need human inspection. The observer requires separate target credentials; legacy shared-auth configurations cannot produce a verified delivery. Target reads use target credentials, and queue notifications use queue credentials.

A stable marker identifies one queue handoff comment per execution. Each poll searches all issue comments before creating or updating it; multiple marker matches stop reconciliation. After a timeout, the next poll searches again before retrying, reducing duplicates without promising exactly-once delivery. Existing terminal status comments are retained. No reminders are enabled. GitHub or local evidence unavailability produces an attention state rather than a fabricated success. Evidence timestamps describe the most recent poll, and no background observation runs when the service is stopped.

Only comments authored by the authenticated queue identity are eligible for update.
Corrupt observation storage is reported as unavailable rather than an empty task
list. `pico watch --all` reads the default dispatcher database and shows a
read-only Delivery attention panel with Needs attention, Waiting on checks and
History views. `--dispatcher-state /absolute/path/state.sqlite3` selects another
database; the existing loopback and control-token protections still apply. GitHub
links open the native review surface; Garden does not approve or merge a PR.

### Quick feedback from the Garden

`pico watch --all --feedback` (or `pico watch . --feedback` for one repository)
adds a **Send feedback** box to each open delivery whose PR was verified. Write a
note, choose *Comment on the pull request* or *Request changes (review)*, then
**Preview**: the Garden shows the GitHub login that will author it (whatever `gh`
is logged in as on this machine), the repository, PR, observed commit and exact
text. **Send to GitHub** re-checks everything immediately before posting and
refuses if anything moved since the preview:

- The PR is always the one the observer verified for that queue issue; the
  browser never supplies a repository, PR number or commit.
- The live PR must still be open, in the same repository, and at the observed
  head. A newer commit refuses the send until the next observation.
- A change request is a `REQUEST_CHANGES` review pinned to the observed commit.
  GitHub does not allow it on your own PR; send a comment instead. A comment is
  not bound to a commit: a push landing in the instant between the check and the
  post is not excluded. A change request on an older commit still blocks merging
  unless the repository dismisses stale reviews.
- Every GitHub call is pinned to github.com; `GH_HOST` is ignored, so the author
  shown in the preview is the account that posts.
- Sent feedback is remembered for the session: the same text cannot be sent the
  same way (comment or change request) to the same commit twice. A GitHub timeout is reported as an unknown outcome:
  check the PR before sending again.
- There is no approve, merge or repair action. A comment grants no authority:
  the observer reports a change request as a human decision, and a repair still
  needs its own explicitly authorized execution.

`--feedback` cannot be combined with `--workspace` (dispatcher deliveries are not
shown there). Like `--controls`, it prints a private URL with a session token; the
token is required for every data read, and writes must come from the Garden page
itself. Opened without the token (for example by reloading, which drops it
from the address bar), the delivery panel says so instead of showing deliveries;
reopen the full URL printed at launch. Sends are recorded in `<state-dir>/garden-feedback.jsonl` with the author,
PR, commit, outcome and a SHA-256 of the text (never the text itself).

Optional `aggregate_ledger` in dispatcher configuration selects an explicitly
initialized launch-admission ledger (see `docs/aggregate-budget-accounting.md`).
Direct `pico exec start` supports `--aggregate-ledger` or
`PICO_AGGREGATE_BUDGET_LEDGER`; workspaces and dispatcher runs use the same central
admission path, so one actual launch is counted once. Dispatcher repairs retain
their root delivery's task identity. `--serialize-global` holds dispatcher starts
while any execution in the same state root is active or uncertain, using the
shared headless launch lock. Workspace parallelism remains separately governed.
This boundary is local cooperative admission, not isolation from a malicious
same-user process. No live ledger or automatic repair policy is enabled by
installing these changes.
