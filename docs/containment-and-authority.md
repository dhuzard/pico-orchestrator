# Containment and authority separation

Design requirements for worker containment (#63) and for the unattended issue
dispatcher (#34). They follow from the trust-boundary summary in
`docs/technical-reference.md` and from #64. This is a requirements document: it
does not claim that any of it is implemented or tested end to end.

## Why policy on the default branch is not enough

#64 makes the default branch's `.pi/autonomy.yaml` the only source of GitHub
write authority, and the default branch now controls authorization for every
future write, so it has to be protected like a credential store. Even so, remote
policy is not a complete boundary while an agent keeps unrestricted GitHub
credentials: an agent that can run `gh` itself bypasses PICO's permission
adapter, whatever the policy says. Closing that gap is credential and filesystem
isolation (#63), not more checks inside PICO.

## OS sandbox versus command filters

| Layer | What it is | What it gives |
|---|---|---|
| OS sandbox (bubblewrap, Seatbelt, container) | Kernel-enforced filesystem, network and credential limits on the worker process tree | Containment: holds even when the agent runs a command PICO never anticipated |
| Command filters (`FORBIDDEN_TOOLS`, prefix deny lists, prompt rules) | Application-level matching on command text | Defense in depth and early, readable refusals. They are not containment: prefix matching misses other spellings of the same action |

Filters stay, but a design or test that relies on them alone does not count as
isolation.

## Containment acceptance tests (#63)

Tests must exercise actual containment, not only performance overhead. Each row
is a deterministic test run with the sandbox on; the attempt and its outcome are
recorded in provenance.

| Test | Expected result |
|---|---|
| Write outside the bound worktree (`$HOME`, another checkout, the parent's `.git/config`) | Denied |
| Read sensitive credential files (`~/.config/gh`, the PICO state directory, Garden token, SSH keys) | Denied |
| Unauthorized `gh` mutation made directly by the worker | Denied |
| Approved local build and test commands | Allowed |
| GitHub publication through the authorized PICO adapter | Allowed |
| Sandbox unavailable while a sandbox is required | Fail closed with a stable error code; an explicit opt-out is recorded on the run |

The two "Allowed" rows guard against a sandbox that is safe only because it
blocks everything. The "Denied" rows are only meaningful if the worker holds no
GitHub credential at all: containment of `gh` means the token is absent from the
worker's environment and unreadable on disk, not that `gh` is filtered.
Publication happens in a process outside the worker sandbox, through the adapter,
which re-reads the default-branch policy (#64).

## Authority separation for the issue dispatcher (#34)

The dispatcher must not read a green CI result as permission to merge, publish or
start unrelated work. This matters most when an LLM chooses its own next task.
Four authorities stay separate, and each is enforced independently, outside the
model's control:

| Authority | Scope | Not implied by |
|---|---|---|
| Planning | Select and queue eligible issues | Execution or publication |
| Execution | Run a bounded, repository-scoped task in a sandboxed worktree under the run lease | Planning (a queued entry is not a started run) |
| Publication | Perform the GitHub operations the default-branch policy permits, through the adapter | Execution succeeding or CI being green |
| Promotion | Advance downstream (`after:`) tasks once the required checks and the merge are verified | CI green alone; in v1 a dependency waits for a merged PR, and PICO never merges |

Requirements:

1. A model may propose queue entries, but only Planning authority can add them,
   and the proposer cannot also grant itself Execution or Promotion.
2. Each dispatched run carries only the Execution scope of its own entry. Starting
   an entry never widens permissions for the next one.
3. Publication is checked per operation against the default-branch policy (#64),
   never inherited from a previous run.
4. Promotion is decided by recorded, verified facts (outcome, review findings,
   merged state), not by the agent that produced the work.
5. A failed, blocked or ambiguous outcome pauses the queue with a reason. It does
   not skip the entry and continue (as #34 already specifies).
6. Each authority's grant is recorded separately in provenance so a run shows
   which authority allowed each step.

## Out of scope here

The lead's own bash and its GitHub access need a separate design (#63 slice 6).
