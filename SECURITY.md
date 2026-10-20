# Security policy

## Supported versions

PICO follows [Semantic Versioning](https://semver.org/). Security fixes go
into the latest minor release of the current major version.

| Version | Supported |
| --- | --- |
| 1.x, latest minor release | Yes |
| 1.x, earlier minor releases | No: upgrade to the latest minor |
| Before 1.0.0 (pre-public history) | No |

Unpinned `pico` runs track the default branch, so they pick up fixes as soon
as they merge. If a project pins PICO in `.pi/orchestrator.lock`, run
`pico update .` to move the pin to a fixed commit.

## Reporting a vulnerability

**Do not open a public issue, pull request or discussion for a security
vulnerability.**

Report it privately through GitHub private vulnerability reporting:

<https://github.com/dhuzard/pico-orchestrator/security/advisories/new>

This opens a private security advisory that only you and the maintainer
([@dhuzard](https://github.com/dhuzard)) can see. Please include:

- the affected version or commit (`pico doctor .` prints the orchestrator
  revision);
- the component, for example the launcher, a worker extension, the Garden or
  the GitHub adapter;
- steps to reproduce, or a proof of concept;
- the impact you expect, and any mitigation you know of.

Remove secrets, tokens and private repository content from logs and traces
before you attach them.

## What to expect

PICO has one maintainer, so these are goals, not guarantees:

- We aim to acknowledge a report within 7 days.
- We aim to send an initial assessment, and a fix plan if the report is
  confirmed, soon after.
- We coordinate disclosure with you. We publish the advisory once a fix is
  available, and credit you unless you ask us not to.

If you have not heard back after 14 days, add a comment to the advisory.

## Scope

PICO orchestrates coding agents on your machine. Reports about the following
are in scope:

- **Workers run with your credentials.** Claude and Codex workers run as you,
  with your environment, your Git and GitHub credentials, and your file
  system and network access. The worker scope checks (the bound worktree and
  the command deny list) are a safety protocol and are best-effort. They are
  not an OS sandbox. A way for a worker, or for prompt-injected repository,
  issue or PR text, to escape its bound worktree, reach secrets or run
  denied Git or GitHub actions is in scope.
- **The launcher runs cached code.** `scripts/use-latest.sh` (the `pico`
  command) fetches this repository into a local cache and runs it. A project
  pin in `.pi/orchestrator.lock` chooses the commit. Ways to make the
  launcher run code that the user did not intend to run, such as through the
  cache, a pin or a runtime snapshot, are in scope.
- **The Garden binds to 127.0.0.1.** `pico watch` serves a local web view,
  and `--controls` adds confirmed actions. Bypassing its Host or Origin
  checks, its per-session token or its confirmations, reading data it should
  not serve, or making it write outside the target checkout are in scope.
- **The GitHub adapter and publication.** Ways to publish, push, merge or
  close something without the autonomy policy or a recorded grant are in
  scope.
- **Provenance and state.** Ways to make secrets end up in run provenance or
  in local state are in scope.

## Out of scope

- The quality, correctness or safety of model output. PICO routes work to
  models, but it does not guarantee what they produce. Report a bad answer as
  a normal issue, not a security report, unless PICO's own safeguards failed.
- Vulnerabilities in Pi, Claude Code, Codex, the GitHub CLI, Git or model
  providers. Report those to the project concerned. If PICO's use of them
  makes the problem worse, that part is in scope.
- Attacks that need an attacker who already controls your user account or
  your machine.
- Running PICO on a shared host where other local users can reach
  127.0.0.1. This is a known limitation of a local web view, but reports
  that make it worse are welcome.
