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

`approve` requires a local interactive terminal and typing the exact task fingerprint prefix. The only other way to approve is the opt-in Garden approval described in [Approving from the Garden over Tailscale](#approving-from-the-garden-over-tailscale), which calls the same approval core after a previewed, identity-bound confirmation. The service itself never self-approves. `--allow-mutation` permits mutations already authorized by the target's autonomy policy; it is *not* limited to code edits by this adapter. **Keep push, PR, merge and deploy disabled in the target policy unless separately reviewing an expanded authority scope.** Do not expose the approval CLI through Moltis or Telegram.

## Approving from the Garden over Tailscale

`pico watch --approvals --dispatcher-config <dispatcher.json>` adds an **Approvals**
panel to the Garden: the open `pico:queued` issues of the queue repository, each
with its number, title, target, objective and acceptance criteria (collapsible),
the first 12 characters of its digest, any reason it cannot be approved, and the
dispatcher's current state for it. Only an issue that was never dispatched (no
approval yet, or `approved` / `stale`) shows **Approve read-only** and **Approve
with code changes** (the Garden's name for `--allow-mutation`).

Both buttons open a preview first. The Garden re-reads the issue with the queue
credential and shows the target, checkout, the mode in bold, the full digest and
the login that will be recorded. Confirming re-reads the issue again and calls
`record_approval`, the same function `approve` uses: the same parse and allowlist,
the same refusal of a dispatched task under `BEGIN IMMEDIATE`, and the same
`approvals` row (no schema change). The confirmation is an HMAC over the action,
issue number, digest, target, checkout, mode and login, recomputed on the live
issue; anything that moved is refused as `CONTROL_PREVIEW_STALE` ("Issue changed,
reload"). As on the terminal, editing the issue later still makes the dispatcher
mark the approval `stale` before launch.

Not available from the Garden: repairs (`--repair-of` stays on the terminal), rejecting
an issue, and starting or retrying a run. Every preview (stage `preview`) and every
confirmed attempt is appended to `<state-dir>/garden-approvals.jsonl` (mode 0600)
with the time, issue, digest, mode, login and result, never the issue text.

### Identity, token and hosts

- Garden still listens on `127.0.0.1` only. Remote access is
  [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve) proxying HTTPS from
  `https://<host>.<tailnet>.ts.net` to the loopback port. Serve sets
  `Tailscale-User-Login` to the tailnet user of the requesting device and replaces
  any copy the client sent. Do not use Tailscale Funnel.
- `--allowed-host <host>.<tailnet>.ts.net` (repeatable) is accepted as `Host` and
  as `Origin: https://<host>.<tailnet>.ts.net`, in addition to the loopback names.
  It needs `--approvals` and `--require-tailscale-login`; Garden refuses to start
  otherwise.
- A request whose `Host` is an allowed remote name is served only with exactly one
  `Tailscale-User-Login` header naming an allowed login, on **every** endpoint: the
  page, its scripts, `/api/runs`, `/api/deliveries`, approvals and feedback
  (`APPROVAL_IDENTITY_REJECTED` otherwise). A remote POST must also carry
  `Origin: https://<that same host>`; a loopback `Origin` on a remote `Host` is
  refused (`CONTROL_ORIGIN_REJECTED`). Loopback requests keep their checks.
- `--require-tailscale-login <login>` (repeatable): every approvals request, reads
  included, must carry exactly one `Tailscale-User-Login` header equal to one of
  these logins, and that login is the recorded actor. A loopback request without
  the header is refused too. Use the login `tailscale whois <phone tailnet IP>`
  prints (with GitHub sign-in it looks like `dhuzard@github`). Without this flag
  (loopback only), the actor is recorded as `local-token`.
- The session token is still required for every data read and every write. It
  stops other local users, who could reach the loopback port and forge the header.
  `--token-file <absolute path>` keeps one token across restarts: a regular file
  owned by the service user with mode 0600, created when missing; a symlink, other
  owner or wider mode refuses to start. The token is then not printed in the log.
  The file must be inside the PICO state directory (`--state-dir`, for example
  `~/.local/state/pi-coding-orchestrator/garden-token`), which worker sandboxes
  cannot read: workers run as the same OS user, so a token elsewhere (such as
  `~/.config/…`) would let a worker drive the Garden. A path outside it refuses to start.
- The `--allowed-host` origin is accepted by `/api/approvals` and, with a verified
  Tailscale login, by `/api/delivery-feedback`, so feedback also works from the
  phone. Workspace controls keep their loopback-only `Origin` check.
- `--approvals` cannot be combined with `--workspace` or `--setup-root`. It can be
  combined with `--feedback`; both share one session token.

### Setup

1. Install Tailscale on the server and the phone, and sign both in to the same
   tailnet: `curl -fsSL https://tailscale.com/install.sh | sh`, then
   `sudo tailscale up`. Enable MagicDNS and HTTPS certificates for the tailnet in
   the admin console. Restrict the tailnet ACL so only your devices reach the server.
2. Find the names: `tailscale status --json | jq -r .Self.DNSName` (drop the
   trailing dot) for the host, and `tailscale whois <phone tailnet IP>` for your login.
3. Write `/etc/pico/garden.env` (no secret in it):
   `PICO_GARDEN_HOST=<host>.<tailnet>.ts.net` and `PICO_GARDEN_LOGIN=<login>`.
4. Review and install `deploy/systemd/pico-garden.service` (its `User=` must be the
   user of `pico-dispatcher.service`: Garden opens the 0600 dispatcher database), then
   `sudo systemctl daemon-reload && sudo systemctl enable --now pico-garden.service`.
   It runs `watch.py --all --approvals --feedback --token-file …` as the service user.
5. `sudo tailscale serve --bg https / http://127.0.0.1:8765` (check it with
   `tailscale serve status`; undo with `sudo tailscale serve reset`).
6. As the service user, read the token: `cat ~/.local/state/pi-coding-orchestrator/garden-token`.
   On the phone, open and bookmark `https://<host>.<tailnet>.ts.net/#token=<token>`.
   The page removes the token from the address bar and remembers it in this browser,
   so later plain links (`https://<host>.<tailnet>.ts.net/`, as in notices) work too.
   Rotate the token by deleting the file and restarting the service; the old one is
   then refused, forgotten by the page, and the `#token=` link is needed again.

This implements the identity requirement of the
[remote Garden ADR](remote-garden-adr.md) for this one action: an approval needs
an allowed Tailscale login plus the session token, an exact preview and a
state-bound, single-use confirmation. Device enrollment, revocation inside Garden
and a read-only monitor role are not built; revoke a lost phone in the Tailscale
admin console and rotate the token.

## Independently supervised systemd service (not enabled)

Only after review, copy `deploy/systemd/pico-dispatcher.service` to `/etc/systemd/system/pico-dispatcher.service`, verify user, paths and installed binaries, then explicitly run. The supplied service units and `deploy/dispatcher.json.template` use the `pico` pilot account and `/home/pico`; adapt all three together if your service user or installation path differs.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pico-dispatcher.service
sudo systemctl status pico-dispatcher.service
journalctl -u pico-dispatcher.service -f
```

The unit has **no dependency on `moltis.service`**. It runs as `pico` in the supplied template, so processes share that account's credentials and filesystem; process independence is not privilege isolation. On service restart, systemd's default control-group termination may interrupt children; the dispatcher records `interrupted` rather than auto-resuming or starting a fresh execution. Recovery requires a separate inspected operator action.

## Current restrictions / follow-ups

- A private queue repository is an optional separate project; this PR neither creates it nor changes target projects.
- No automatic issue discovery beyond operator approval (`approve --issue N`). Approved jobs are stored in SQLite and polled. Only one runs at a time; it does not yet implement #34 dependencies or merge-gating.
- No automatic draft PR, deployment, permission escalation, task creation or autostart on installation. No automatic merge unless a target opts in to `delivery_autopilot` (see [Automatic audit and merge of deliveries](#automatic-audit-and-merge-of-deliveries)).
- Status is written as GitHub issue comments when possible. Comment delivery is retried on failure, but a crash between comment and local checkpoint can duplicate a status comment.
- Task authorization is a local exact-title/body digest, not a cryptographic statement about human identity against a compromised same-user process. Store state and credentials with strict permissions. Any lead capable of reading the local state or GitHub credentials remains part of the threat model.
- `interrupted` is terminal for this prototype; no unattended retries, no resume, no automatic expansion of permissions. A new issue and new explicit approval is required for a new execution attempt.
- `gh issue list` is not used for authorization; non-operator comments, labels and issue contents never grant execution.
- Integrate #34, #63 and #87 before calling this a production unattended coding system. Add a real WSL/Windows sign-in and service restart pilot, with mock-agent tests first.

## Delivery observation and human handoff

Completed, partial, blocked and interrupted executions are reconciled on every service poll, including after restart. The execution outcome remains unchanged; a separate SQLite delivery projection records the observed publication, PR head, CI, human review, attention reason and next action. `status` includes this projection in each task's `delivery` field. The observer only reads GitHub and local provenance; it never starts a model, repairs code, approves, merges or deploys. Repairs require a separate explicitly authorized execution; no comment grants authority.

The service needs GitHub CLI 2.48 or newer (`gh api --paginate --slurp`); `serve` and `once` refuse to start with an older `gh` (Ubuntu 24.04 packages 2.45). Evidence or handoff-comment failures are written to the service journal once per distinct cause.

A blocked, partial or interrupted execution shows its blocker code as `Execution <state>: <CODE>`. When the lead process exits without recording an outcome, the run settles as `BLOCKED`. If no phase recorded a blocker and the lead model's last call failed on a usage quota or rate limit (after pi's own retries), the code is `MODEL_QUOTA_EXHAUSTED`. The next action is `Retry after <UTC time>: open a new queue issue and approve it`, using the reset time when the provider's error states one. Otherwise it says the reset time was not reported. Any other silent exit stays `ORCHESTRATION_OUTCOME_UNRECORDED`. Only the cause and the reset time are stored, in the parent record's `lead_stop`; the provider's error text never is. Nothing retries automatically.

Configure expected check names for each target, for example:

```json
"delivery_expected_checks": {"dhuzard/oratlas": ["build", "test"]}
```

A nonempty list is required to establish passing expected checks. Each expected check must pass; a skipped, neutral, cancelled or failed expected check fails the observation. Other checks fail it only by actually failing, so conditional jobs skipped on pull requests do not. Check results come from the pull request's check rollup, which needs the Checks permission. Fine-grained personal access tokens cannot hold it, so when the rollup is refused the observer reads the latest attempt of each GitHub Actions job and the commit statuses for the exact PR head instead (target token: Actions and Commit statuses read-only), recorded as `checks_source: actions`. Checks from other GitHub Apps are invisible there, so an expected check provided by one stays missing and never counts as passed. It is an operator-maintained expectation, not a replacement for GitHub branch rules. The observer also reads branch protection and includes any accessible required contexts; inaccessible protection or rulesets leave merge readiness unknown. Passing expected checks invites a person to review the PR; it does not mean the PR is mergeable or satisfy unknown GitHub rules. Missing, skipped, neutral, cancelled or failed checks never count as passed. A changed head during polling invalidates the observation. Only reviews by GitHub `User` accounts at the observed head count as human decisions; a dismissed or older approval does not count as current.

An applied PR creation or observed publication receipt must appear in a linked publication child for the approved target. An applied `github_integration.py --publish --parent-run-id` links its own run to the parent before any remote write and reports `parent_linked`, so the link does not depend on the lead recording a child event; a publication for a parent that is already terminal cannot be linked and needs human inspection. The PR's base and head repositories must both match the target, and its current head must match integrated evidence in parent provenance. New publication receipts retain the exact commit, branch, parent run and operation stage before remote writes, allowing the final head to be identified across multiple waves. Legacy records without a receipt require one unique integrated head. Later commits, missing child provenance and cross-repository PRs need human inspection. The observer requires separate target credentials; legacy shared-auth configurations cannot produce a verified delivery. Target reads use target credentials, and queue notifications use queue credentials.

When GitHub cannot be read, the delivery records `checks: unavailable` with one of a fixed set of causes. The cause goes in `evidence.observation_error`, together with `observation_operation` (the `gh` subcommand: `api` or `pr view`) and `observation_http_status` (or `none`). The reason and next action shown in `pico watch` and in the queue comment depend on the cause:

| Cause | Typical trigger | Next action |
| --- | --- | --- |
| `auth` | HTTP 401/403, bad credentials, resource not accessible by the token | Check the target token's permissions and expiry |
| `not_found` | HTTP 404, or GraphQL cannot resolve the pull request | Check the pull request still exists and the target token can see the repository |
| `rate_limited` | HTTP 429, or a primary or secondary rate limit (also reported as 403) | Wait for the rate limit to reset; the next poll retries |
| `gh_too_old` | `gh` rejects a flag or subcommand, such as `--slurp` | Upgrade gh on the dispatcher host (delivery observation needs gh >= 2.48.0) |
| `gh_missing` | The configured `gh` cannot be started | Install gh or fix the configured gh path |
| `network` | Timeout, DNS or connection failure, or HTTP 5xx | Check network access to GitHub; the next poll retries |
| `invalid_response` | GitHub answered, but the data does not match the pull request | Inspect the pull request in GitHub, then wait for a fresh observation |
| `unexpected` | Anything else | Restore target access and retry |

Classification fails closed: an unmatched failure is `unexpected`. The `gh` message is never stored or shown, because it can contain URLs or response text; only the three fields above are kept. The service journal logs each distinct cause once per issue, for example `Issue #7: GitHub observation unavailable: auth (gh api, HTTP 401)`. It logs again when the cause changes, or when the cause comes back after a successful observation.

A stable marker identifies one queue handoff comment per execution. Each poll searches all issue comments before creating or updating it; multiple marker matches stop reconciliation. After a timeout, the next poll searches again before retrying, reducing duplicates without promising exactly-once delivery. Existing terminal status comments are retained. No reminders are enabled. GitHub or local evidence unavailability produces an attention state rather than a fabricated success. Evidence timestamps describe the most recent poll, and no background observation runs when the service is stopped.

Only comments authored by the authenticated queue identity are eligible for update.
Corrupt observation storage is reported as unavailable rather than an empty task
list. `pico watch --all` reads the default dispatcher database and shows a
read-only Delivery attention panel with Needs attention, Waiting on checks and
History views. `--dispatcher-state /absolute/path/state.sqlite3` selects another
database; the existing loopback and control-token protections still apply. GitHub
links open the native review surface; Garden does not approve or merge a PR
(a target with `delivery_autopilot` is merged by the dispatcher, not by the Garden).

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

## Automatic audit and merge of deliveries

For a target listed in `delivery_autopilot`, there is no human merge gate: an
automated audit decides, a pass merges the pull request, and the person is told
afterwards (activity log, Garden, Telegram). Nobody has to read the code. A target
that is not listed behaves exactly as before: the dispatcher never merges.

```json
"provider_env_file": "/home/pico/.config/pi-coding-orchestrator/provider-api.env",
"delivery_autopilot": {
  "Neuronautix/metadatapp": {"merge_method": "merge", "model": "claude-opus-5-5", "max_diff_bytes": 150000}
}
```

The three per-target settings are optional and default to the values shown. The
target must also have `delivery_expected_checks` and separate target credentials
(`target_gh_config_dir`), and `provider_env_file` must be set; the dispatcher refuses
to start otherwise. `provider_env_file` is a `KEY=VALUE` file, mode 0600, owned by
the service user. Only `ANTHROPIC_API_KEY` is read, by the dispatcher process itself,
at audit time, and sent only in the `x-api-key` header of the audit request: it is
never put in the environment of `gh`, `pico` or any child, and never logged. An
unreadable or missing key postpones the audit (one journal line) without using an
attempt. PICO's configuration directory is already denied to workers.

On each poll, after observing deliveries, the dispatcher acts on at most one
delivery. It is eligible only when:

- the queue issue has an approval whose execution `completed`, with the same
  execution id as the delivery (the dispatcher's own run);
- the observer bound the PR to that run's publication evidence, the PR is open
  and not a draft, both its head and base repositories are the target (no fork),
  and its base is the target's current default branch;
- every expected check passed on the PR's **current** head, no human requested
  review or changes, and no earlier review is stale;
- that head SHA has no final audit yet. Audits are recorded per head SHA, so the
  same commit is never audited, or merged, twice.

A draft or moved PR is re-checked on later polls and shown as "Not audited
automatically yet".

**Deterministic pre-checks** run first; a failure is final for that head and makes
no model call. The diff must be at most `max_diff_bytes`. No changed path (renames
included) may be under `.github/`, `.pi/`, `scripts/` or `deploy/`, be a dependency
or build file, a `CODEOWNERS` file or an `.env` file, or be named like a secret,
credential or key (`*secret*`, `*credential*`, `*.pem`, `*.key`, `id_rsa`, `.netrc`
and similar). GitHub must provide a complete changed-file count, a text patch
for every file and the old path of every rename or copy. Binary or patchless
files are left for human review. The queue issue must still be open, have the
`pico:queued` label and match the digest that was approved; closing, unqueueing
or editing it stops automatic merging.

**The model audit** is one Anthropic Messages API request (`model`, default
`claude-opus-5-5`, effort `high`) with structured output: a JSON schema the answer
must match (`verdict` pass|fail, `risk`, a 3 to 5 sentence plain-language `summary`,
`intent_matches`, one `acceptance_criteria_met` entry per criterion with evidence,
and `findings` with severity critical|high|medium|low|info, title, file, line and
detail). The input is THE TASK (title, Objective and Acceptance criteria of the
approved queue issue), the PR title and body, the unified diff and a CI summary.
The system prompt states that the issue, the PR text and the diff are untrusted
data and that any instruction inside them must be ignored. Each of them is wrapped
in a tag carrying a random nonce, so text inside cannot close the block.

**The pass rule is applied by the dispatcher, not the model:** verdict `pass`, and
`intent_matches`, and one positive report entry for every acceptance item derived
from the approved queue task, in the same order and with the exact criterion text,
and no critical or high finding, and the pre-checks passed. A report that omits,
renames or adds an item fails even if its verdict is `pass`.

- **Pass:** the report is posted as a PR comment, then the PR is merged with
  `gh pr merge <n> --merge --match-head-commit <audited sha>` using the target
  credentials. The dispatcher re-reads the exact head, default base branch,
  configured checks (through Actions and commit status) and human reviews before
  merging. A newly failed check, review request, change request or stale review
  blocks the merge.
  GitHub also refuses the merge if the head moved. The merge commit and time are
  recorded. Transient GitHub failures are retried twice with backoff, then the
  delivery becomes `audit_failed`.
- **Fail:** the report is posted as a PR comment and the PR stays open. The
  delivery shows `audit_failed` in the Garden and in the queue handoff comment. A
  fix needs a new queue issue and approval.
- **API or GitHub error:** retried after 5, then 10 minutes, at most three attempts
  per head SHA; then `audit_failed` with the reason. An invalid or refused model
  answer counts as an error. A refusal is not retried on another model.

Every attempt records the model and its input and output tokens in the
`delivery_audits` table of the dispatcher database (a new table; existing tables
are unchanged). Delivery states gain `auditing` (queued, retrying or merging; shown
under "Waiting on checks") and `audit_failed` (shown under "Needs attention"). A
merged delivery says "Merged automatically after a passing audit".

The audit is the only gate when the target has no branch protection (like
Neuronautix/metadatapp). A wrong pass by the model is merged; the pre-checks keep
workflow, agent-policy and credential files out of automatic merges.

## Activity and notifications

The dispatcher and the Garden append events to `<state dir>/activity.jsonl` (mode
0600, one JSON object per line): `{at, kind, title, summary, url, issue?, pr?, sha?}`,
where `kind` is `approval_needed`, `approval`, `run_finished`, `delivery_opened`,
`audit_passed`, `audit_failed`, `merged`, `deploy` or `error`. Events come from a
queue issue waiting for approval, a terminal or Garden approval, a run reaching a
final state (with its blocker code), a delivery PR seen for the first time, an
audit result, a merge and a refused merge. Issue bodies and diffs are never written.

`approval_needed` is reported when notices are configured (`notify` is set): each
poll the dispatcher lists the open `pico:queued` issues once more and, for each one
that parses as a task for an allowed target and has no approval (or whose approval
went stale because the issue changed), records one event per issue and digest:
title "Approval needed: #N <title>", summary the target and the first 200
characters of the Objective, URL the Garden (`notify.garden_url`) or else the
issue. An edited issue has a new digest and is announced again; an invalid,
approved or dispatched issue is not. Announcements are remembered in the
`activity_marks` table. This never approves anything.

`pico watch --approvals --dispatcher-config …` serves the latest 50 events, newest
first, at `GET /api/activity`, and shows them in a **Recent activity** panel at the
top of the page. The endpoint needs the session token; on an `--allowed-host` name it
also needs the allowed Tailscale login, like every endpoint.

Optional Telegram notices, send-only:

```json
"notify": {
  "telegram_env_file": "/home/pico/.local/state/pi-coding-orchestrator/telegram.env",
  "garden_url": "https://pico-pilot.tail35e544.ts.net/"
}
```

The file holds `PICO_TELEGRAM_BOT_TOKEN` and `PICO_TELEGRAM_CHAT_ID`, mode 0600,
owned by the service user (a symlink is refused). Each event becomes one plain-text
`sendMessage` to that chat: title, summary and URL. Pico never calls `getUpdates`
and never acts on a message sent to the bot. A failed notice is logged (without the
token) and never blocks the dispatcher. Keep the file in the state directory, which
workers cannot read.

`garden_url` (optional) is the Garden's address as the phone opens it. It must be an
`https` URL without credentials, query or fragment, so it can never carry the
session token. When it is set, the notice of an event that asks you to look or act
ends with a `Garden: <garden_url>` line: `approval_needed`, `audit_failed`, `error`,
`delivery_opened`, and `run_finished` unless the run completed. The event's own URL
(the issue or PR) is unchanged. The link has no `#approvals`-style anchor: the page
uses its fragment for the token and the run view, and its panels load after the
page. The link works without a token once this browser has opened the
`#token=` link (step 6 above).

External tools add events with:

```bash
python3 tools/activity.py emit --config ~/.config/pi-coding-orchestrator/dispatcher.json \
  --kind deploy --title "Deployed abc1234" --summary "Pico abc1234 is live." \
  --url https://github.com/dhuzard/pico-orchestrator/commit/abc1234
```

It appends the event and sends the notice; `--url` is optional and must be an
absolute `https` URL. Run it as the service user (for example `sudo -u pico …` over
SSH): the Telegram file must be owned by the user reading it, and a log created by
another user could not be read by the Garden. `deploy/dispatcher.json.template` shows a complete configuration.
