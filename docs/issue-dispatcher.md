# Pico GitHub Issues dispatcher (opt-in prototype)

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

Clone the reviewed Pico commit into `/home/dhuzard/src/pico-orchestrator` (or adapt paths). Ensure that `pico`, `gh`, `pi`, and the desired Claude/Codex backends run for the same service user. Authenticate `gh` for the private queue and only the permitted target repositories. Restrict the credential scope to what the chosen run and status path needs; never place tokens in an issue.

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
  "gh_executable": "gh",
  "pico_executable": "pico"
}
```

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
