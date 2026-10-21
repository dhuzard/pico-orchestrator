# Open-source readiness audit — 20 October 2026

Scope: `dhuzard/pico-orchestrator` at `97bea69` (v1.0.0 plus #11), one day
after the first public release. This audit covers security, code quality,
tests, open-source practice and product direction. It changes no runtime code.
Findings that cite a file and line were checked against the code. Those
marked *reproduced* were also executed.

## Verdict

The engineering core is strong. It fails closed with stable blocker codes,
routing is deterministic behind a single evaluator, the `gh` argv allowlist is
tight, publication needs a reviewed plan hash, and the viewer never uses
`innerHTML`. All 90 Node tests and 568 pytest tests pass on Ubuntu and macOS
CI, and `npm audit` is clean.

The gaps are in what a public project needs around that core:

- There are no community or security files.
- The README assumes the reader already knows Pi.
- The documented validator fails on `main`.
- Worker isolation is weaker than the docs suggest.

All of them are fixable in about a week.

## Status update — later on 20 October 2026

Changes on `main` since the audit:

- **#17** (route escalation provenance) fixed the validator's README check.
  `python tools/validate_routing.py` now passes, so Q1 is reduced to "not run
  in CI".
- **#19** (watch argument forwarding) merged. Both PRs touched audited files;
  no other finding changed.
- **#18** (workspace task objectives) and **#20** (`pico report`, which files
  a bug in the PICO repository through a plan-hash dry run) were green and are
  being merged once their branches are up to date with `main`. Branch
  protection requires this.

Remediation is underway in four parallel workstreams. Each one runs the
checks listed in AGENTS.md:

| Workstream | Findings |
| --- | --- |
| A. CI gates and supply chain | Q1 (validator in CI), Q10 (ruff), S6 (SHA pins, `persist-credentials: false`), Dependabot, CodeQL, `engines` and `.nvmrc` |
| B. Storage safety and Garden | Q3 (shared atomic writer), S3 (symlink write), Q2 (headless lock), S4 (token on read endpoints), S5 (Content-Length), Q4 if it stays small |
| C. Community and newcomer docs | SECURITY, CONTRIBUTING, CODE_OF_CONDUCT, CODEOWNERS, issue contact links, stale references, README newcomer path |
| D. Worker and launcher hardening | S1 (worker env allowlist, deny-list wording, sandbox follow-up), S2 (pins must be on release history, warning when a pin changes) |

These remain for later: signed release tags (they need a maintainer signing
key), `pico demo`, the mission dispatcher (A1), the run lease (A3), Q5 to Q9,
and the feature roadmap below.

### Remediation result

All four workstreams are integrated on `claude/pico-open-source-audit-12i2v8`
together with `main` after #18, #19 and #20.

| Finding | Status |
| --- | --- |
| Q1 validator in CI | Fixed. `validate_routing.py` runs in CI. |
| Q10 lint | Fixed. `ruff` (E9, F, B) runs in CI, and all 17 findings are fixed (one intentional `noqa` with its reason). |
| S6 action pinning | Fixed. Actions are pinned to SHAs, each checked against its upstream tag, with `persist-credentials: false`. CodeQL and Dependabot are added. Neither has run on GitHub yet. |
| S3 Garden symlink write | Fixed. Symlinked `.pi` and `autonomy.yaml` are refused with `AUTONOMY_POLICY_PATH_UNSAFE`. Writes go through the shared atomic writer. Regression tests reproduce the attack. |
| S4 token on reads | Fixed as the finding scoped it (#33). With `--controls` on, `/api/controls`, `/api/branches`, `/api/runs`, `/api/workspace` and the new `/api/issues` need the token, and the viewer sends it on every poll. The static page, script and stylesheet stay open, because the token reaches the page only in the URL fragment. Without `--controls` no endpoint needs a token, so a plain `pico watch` on a shared host still serves run metadata, and now open issue titles, to any local user. |
| S5 Content-Length | Fixed. A malformed value returns 400 `CONTROL_BODY_INVALID`. |
| Q2 headless lock | Fixed. Locks are held across start, status and cancel. A concurrency regression test fails without the lock. |
| Q3 atomic writes | Fixed for the five fixed-temp-name writers. The four `mkstemp` writers are unchanged; they lack `fsync` but are otherwise safe. |
| Q4 state directory | Open. Unifying it would change provenance's repo-local fallback, so it needs a maintainer decision. |
| S1 worker isolation | Mitigated. Workers get an allowlisted environment with credentials withheld, and dropped variable names are recorded. More `git`/`gh` bypass forms are denied. The Claude Code sandbox can be enabled with `PI_ORCHESTRATOR_WORKER_SANDBOX=1`. It has not been run against a real worker, so it stays off by default. Workers still load the target repository's own `.claude/settings.json`, hooks included; that needs a follow-up review. |
| S2 launcher pins | Mitigated. A repository pin must be on the default branch, in a release tag, or approved with `pico pin` for that checkout; otherwise `PIN_NOT_ON_RELEASE_HISTORY`. Pin changes print a warning. Signed tags and protection against downgrades along the default branch are still open. |
| Community files | Added: SECURITY, CONTRIBUTING, CODE_OF_CONDUCT, CODEOWNERS and issue contact links. Discussions and private vulnerability reporting must be turned on in the repository settings for the links to work. |
| Stale references | Fixed. `issue #5` was correct, and now links to the public issue. |
| README newcomer path | Added: what PICO and Pi are, a diagram, prerequisites (Python 3.10+, Node 24), and a docs index. The large advanced sections have not moved yet. |

Behaviour changes users may notice:

- `pico models set` refuses a symlinked user model config.
- Opening the Garden with controls on but without the token hides the control
  panels.
- A repository pin outside the release history fails closed until it is
  approved with `pico pin`.

## Checks run

| Check | Result |
| --- | --- |
| `npm ci && npm run check` (tsc and 90 Node tests) | Pass |
| `python -m pytest -q tests` | 568 passed, 322 subtests, 167 s |
| `npm audit` | 0 vulnerabilities |
| `python3 tools/validate_routing.py` (AGENTS.md says to run it) | **FAIL**, reproduced: "README.md contains an environment-specific absolute path" |
| `ruff --select E9,F,B` (excluding E501) | 17 findings: 3 unused imports, 2 B904, 1 B017, 8 B905 (all 8 checked, lengths verified) |
| `mypy tools` | 117 errors, mostly dict-literal inference; no real bug found |
| `bash -n scripts/*.sh` | Pass |

## Security

There is no high-severity injection bug. Subprocess calls never use
`shell=True`, YAML is loaded with `safe_load` (the routing loader also rejects
duplicate keys), and IDs are regex- or schema-checked before they are joined
into paths. The Garden checks Host, Origin, a token and an HMAC confirmation.
Provenance is scanned for secrets.

| ID | Sev | Finding | Fix |
| --- | --- | --- | --- |
| S1 | Medium | **Worker scope is advisory.** Full-mode workers get unrestricted `Bash` and `Agent` (`extensions/claude-worker.ts:22`). The deny list is matched by prefix (`:30`), so `git -C . push`, `env git push` or `curl … $GH_TOKEN` pass it. The worker also inherits the full environment (`env: process.env`, `:110`). The scope check only snapshots the parent checkout, so writes to `$HOME`, to other repos or to the network go unseen. Prompt-injected issue or README text could exfiltrate tokens. | Pass an env allowlist to the worker. Run it under an OS sandbox (Claude Code sandbox, bwrap or a container) limited to the worktree, with network off by default. Document the deny list as best-effort. |
| S2 | Medium | **The installer auto-updates without integrity checks, and the target repo picks the version.** Unpinned runs do `fetch` + `reset --hard origin/<default>` and then execute (`scripts/use-latest.sh:243-280`). Nothing verifies a signature. `.pi/orchestrator.lock`, which may come from an untrusted repo, can pin any fetched commit, which allows a downgrade (`:127-149`). | Sign release tags and `git verify-tag` before building a runtime. Restrict pins to tagged releases or ancestors of the default branch. Warn when a repo pin changes from the one the user last approved. |
| S3 | Low-Med | **Garden `set_permission` follows symlinks.** It writes a fixed temp name, `.pi/autonomy.yaml.garden-tmp` (`tools/garden_controls.py:447`), using `write_text` followed by `os.replace`. *Reproduced:* a committed symlink at that name caused an arbitrary user-writable file to be overwritten once the user confirmed a permission change. | Create the temp file with `mkstemp(dir=…)`. `lstat` `.pi` and its contents and refuse symlinks. Assert that the resolved path stays under the checkout. |
| S4 | Low | Read endpoints `/api/controls`, `/api/branches` and `/api/runs` need only a valid Host, with no token (`tools/watch.py:471-497`). They are bound to 127.0.0.1, so this matters only on shared hosts. | Require the token for these endpoints when `--controls` is on. |
| S5 | Low | A non-numeric `Content-Length` raises outside the `try` (`tools/watch.py:454`). The request crashes and a traceback is logged. | Catch the error and return 400. |
| S6 | Low | CI actions are pinned by tag (`@v4`), and `checkout` keeps `persist-credentials: true`. `permissions: contents: read` limits the impact. | Pin actions to SHAs. Set `persist-credentials: false`. Have Dependabot keep the pins current. |

Two items from `docs/audits/autonomous-reliability-2026-10-12.md` are still
open and also bear on safety: **A3**, no single-owner lease when two Pi
processes resume the same run, and **A10**, no revision checkpoint on resume.

## Code quality

### Must fix

- **Q1. The documented validator fails on `main`, and CI does not run it.**
  `ABS_PATH_RE` (`tools/validate_routing.py:90`) matches the second slash of
  `https://raw.githubusercontent.com/...` (README.md:15) and also
  `/usr/bin/pico` (README.md:25). Fix the regex and add
  `python tools/validate_routing.py` to CI. A gate that is broken in plain
  sight undermines the "fail-closed" message.
- **Q2. The headless execution record is written without a lock.**
  `save_record` (`tools/headless_run.py:111-116`) writes through a fixed
  `.json.tmp` name, and `launch` and `cancel` both read, modify and write the
  record. Two launches with the same id can both start Pi. Reuse the `flock`
  pattern from `workspace_run.py:84-94`.

### Should fix

- **Q3. One atomic-write helper.** There are 8 separate implementations.
  4 use unique `mkstemp` names and 5 use fixed temp names (headless_run,
  workspace_run, garden_controls, context_budget, model_profiles). None
  calls `fsync`. A shared `atomic_write_text/json` would also fix S3.
- **Q4. One state-directory resolver.**
  - `provenance.py:552` falls back to repo-local `provenance/runs`.
  - `mission.py` and `headless_run.py` fall back to XDG.
  - `watch.py:552` also reads `PI_CODING_ORCHESTRATOR_STATE_DIR`.
  - With no env var set, a run written by checkout-local provenance does not
    appear in `watch`.
- **Q5. One loader and one subprocess wrapper.** `yaml.safe_load` is called
  directly in 7 modules, so autonomy, mission, backlog and workspace files
  lose the duplicate-key rejection. About 9 `_run`/`_git` wrappers and 6
  `_now` helpers repeat the same code.
- **Q6. Packaging.**
  - There is no `pyproject.toml`, `requires-python`, `engines` field or
    `.nvmrc`.
  - 19 tools and 22 tests use `sys.path.insert`.
  - A `pico_tools` package with `python -m` entry points would remove the
    path hacks and the E402 warnings, and would allow `pipx install`.
- **Q7. Module size.**
  - `routing_engine.py` (2360 lines) already has section banners. Split it
    into loaders, expressions, plan validation (about 1000 lines), schema
    validation, and resolve.
  - `provenance.py` (1642 lines): split into telemetry, validators, store,
    and trace rendering.
  - `orchestration-lifecycle.ts` is mostly a single 900-line closure. Move
    the tool guards, result handling and wave tools into their own modules.
- **Q8. TypeScript duplication.**
  - `claude-backend-adapter.ts:14-30` repeats the model-ID lookup from
    `backend_preflight.py:139-144`.
  - Four separate Python-spawn helpers disagree on the env var
    (`PI_MISSION_PYTHON`).
  - The core routing is correctly *not* duplicated.
- **Q9. Tests.**
  - `viewer/app.js` (851 lines) has no tests. Add a Playwright smoke test
    against a fixture state directory.
  - The suite is a long tail of git fixtures with no single slow test.
    `pytest-xdist` would bring 167 s down substantially.
  - The `time.sleep` polling in `test_headless_run.py` is a flake risk.
- **Q10. Lint gate.** Add `ruff` (E, F, B, I, UP; long lines ignored) to CI
  and fix the 17 findings. Most can be fixed automatically.

## Open-source practice

| Item | State | Action |
| --- | --- | --- |
| LICENSE (MIT), CHANGELOG (Keep a Changelog), v1.0.0 release and tag, issue forms, PR template, CI matrix | ✅ Present and good quality | — |
| `SECURITY.md` | ❌ Missing | **Must.** Enable GitHub private vulnerability reporting and document the supported versions and response time. This matters for a tool that runs agents with credentials. |
| `CONTRIBUTING.md` | ❌ Missing | **Must.** Cover dev setup (the AGENTS.md checks), the test commands, the routing-change rules, the DCO or no-CLA stance, and the review policy. |
| `CODE_OF_CONDUCT.md` | ❌ Missing | **Must.** Contributor Covenant 2.1 with a contact address. |
| `.github/dependabot.yml` | ❌ Missing | **Must.** Cover npm, pip and github-actions. |
| `.github/CODEOWNERS` | ❌ Missing | Should. Cover `routing/`, `schemas/` and `scripts/use-latest.sh`. |
| CodeQL or Scorecard workflow | ❌ Missing | Should. CodeQL (Python, JS) and the OpenSSF Scorecard badge. |
| Branch protection and signed tags | Issue #5 closed | Should. Sign tags (this also enables S2). |
| Repository topics, homepage, Discussions | No topics; Discussions off; the wiki is enabled and empty | Should. Add topics such as `ai-agents`, `claude-code`, `codex`, `orchestration`, `pi`, `provenance`. Turn on Discussions for Q&A, and turn off the empty wiki. |
| Issue config | `contact_links: []` | Should. Link Discussions and SECURITY.md. |
| Labels | — | Should. Add `good first issue` and `help wanted`, and seed 3–5 starter issues (for example Q3, Q5, Q10, S5). |
| Stale private-history references | README:120 "issue #49", technical-reference:1257 "issue #11", releases/v1.0.0:135 "issue #5" all point at the old private tracker; `backlog/backlog.yaml` names `dhuzard/pi-coding-orchestrator` and cites commit SHAs not present in the public history | **Must.** Rewrite them, or mark them as historical. Dead links in a day-one repo look abandoned. |
| `package.json` | `"private": true`, version 1.0.0 | Fine for now. Add `repository`, `license` and `engines`. |
| README for newcomers | 892 lines, 50 KB. It never says what Pi is, never links `@earendil-works/pi-coding-agent`, and lists no prerequisites or versions. Six advanced workspace sections come before "What it does". | **Must.** Put a one-paragraph pitch, a diagram, prerequisites, a 60-second demo and a link to the docs at the top. Move the workspace, policy and GitHub sections into `docs/`, and consider a docs site (MkDocs or Starlight on Pages). |

## Feature suggestions

### Must-have (adoption blockers)

1. **Sandboxed workers by default** (S1). The first question any security-minded adopter will ask is "what can a worker touch?" Provide an env allowlist, an OS sandbox limited to the worktree, and an opt-in network allowlist. Then the claim that workers can only write inside their bound worktree becomes enforced rather than advisory.
2. **`pico demo`, a zero-credential first run.** One command creates a throwaway repo and runs `/orchestrate` with recorded or fake backends. Garden opens with a live run, so a newcomer sees routing, waves, provenance and Garden in under 2 minutes without Claude, Codex or GitHub auth. This is the most important thing for adoption.
3. **Verified, versioned installs** (S2). Provide `pico install --version v1.x`, signed tags, and `pipx` or Homebrew distribution (Q6). Default to the latest *release* rather than the tip of `main`.
4. **A durable mission dispatcher** (open item A1 from the previous audit). Without it, `/deliver` does one turn and stalls. It needs leases (A3), bounded attempt budgets, and a bounded review → repair → re-verify loop.

### Should-have (differentiators)

5. **A policy simulator, plus a CI Action for route diffs.** Running `pico route diff old.yaml new.yaml` would replay the stored, classified tasks from past provenance and show which ones change route, capacity or reviewer. Ship it as a GitHub Action that comments the diff on any PR that touches `.pi/routing.yaml`. Deterministic routing makes this possible, and few other harnesses can offer it.
6. **An evidence scoreboard per route.** Aggregate *observed* outcomes per route and capacity from provenance: review findings, repair loops, CI pass on the first push, and recorded tokens, with unknowns shown as unknown. Policy tuning is then grounded in your own runs, and nothing is fabricated, which AGENTS.md requires.
7. **A portable run report.** `pico report <run>` exports a self-contained HTML or Markdown provenance summary (route, workers, reviews, checks and PR links) that `publish` can attach to the draft PR. Reviewers see how a PR was produced.
8. **An issue → draft PR mode in GitHub Actions.** Applying a `pico` label to an issue triggers a bounded `pico exec` and opens a draft PR with the run report attached. This reuses the existing headless, autonomy-policy and plan-hash machinery.
9. **Garden as a cockpit.** Add approve or pause at each wave boundary, cancel a worker, and keyboard-only operation. It would use the existing HMAC confirm flow.

### Killer bets (longer term)

10. **PICO routing as an MCP server.** Expose `route_task`, provenance and worktree scoping over MCP, so that Claude Code, Codex CLI or any other harness can use PICO's deterministic routing and provenance without Pi. This widens the potential audience well beyond Pi users.
11. **A pluggable backend interface.** Today the model is Claude/Codex. A documented adapter contract (preflight, identity pinning, capacity tiers, telemetry) would allow Gemini, local models through Ollama, or OpenRouter, with the same fail-closed preflight.
12. **Signed provenance (in-toto/SLSA-style attestations) for agent-authored commits.** A verifiable record of which policy, route and models produced a PR, and who reviewed it. As agent-written code comes under supply-chain scrutiny, this is a distinctive position, and the provenance layer already provides most of what it needs.

## Suggested order

1. **Day 1–2.** Fix Q1 and add the validator and ruff to CI. Add SECURITY, CONTRIBUTING, CODE_OF_CONDUCT and dependabot. Fix the stale references. Set repository topics and Discussions.
2. **Week 1.** Fix S3, S5, Q2 and Q3 with one shared atomic-write helper. Restructure the README top section. Open starter issues.
3. **Weeks 2–4.** S1 sandboxed workers; S2 verified installs; `pico demo`; Q6 packaging.
4. **Next.** The policy simulator Action, the run report, the mission dispatcher, then the MCP server.
