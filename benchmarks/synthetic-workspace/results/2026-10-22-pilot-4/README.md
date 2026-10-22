# Pilot #4: real multi-repository run, 2026-10-22

The first real-agent run of the full four-task synthetic workspace, on sandbox repositories. Every number here comes from a recorded run (`tools/benchmark_metrics.py`). Nothing is estimated, and n is small: one full run, plus one earlier first-wave attempt and one interrupt/resume run.

## Setup

| | |
| --- | --- |
| PICO runtime under test | `c7492f895adc78298be949867fb3e853d75bac22` (main after #64), `PI_CODING_ORCHESTRATOR_REF` |
| Kit | branch of #66 (sandbox mode, `--max-tokens`, `--allow-mutation`, qualification over both repositories, synth-lib CI fix) |
| Repositories | `dhuzard/synth-lib`, `dhuzard/synth-app` (public, disposable). Pins: synth-lib `59c06cb`, synth-app `260cd70`. Issue #1 in each |
| Sandbox policy (default branch) | `create_worktrees`, `commit`, `push_branch`, `create_pr` on; `create_github_issues`, `comment_on_issue`, `comment_on_pr`, `mark_pr_ready`, `merge`, `deploy` off |
| Models | default tiers, not configured: lead `openai-codex/gpt-6.1-sol`, Claude worker `sonnet`, Codex reviewer through `pi-subagent` (unpriced) |
| Budget | `max_tokens: 120000` for wave 1, raised in the manifest to `1000000` after the hold was observed |

```bash
PICO_REAL_BENCHMARK=1 benchmarks/synthetic-workspace/run-real.sh c7492f895adc78298be949867fb3e853d75bac22 pilot4b \
    --output-dir ~/pico-bench/pilot4b --owner dhuzard --publication --i-own-these-repos --max-tokens 120000 \
    --issue lib-slugify=1 --issue lib-stopwords=1 --issue app-post-url=1 --allow-mutation
# then by hand: pico workspace publish (dry run, then --apply --plan-hash), ci, promote; pico workspace schedule --allow-mutation
```

## Outcome (`full-run.json`)

| Task | Repository | Result | Billed tokens | Cost (lower bound) | Wall-clock | Lead calls | `orchestration_record` calls |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `lib-slugify` | synth-lib | COMPLETED, verified | 206,260 | $0.80 | 210 s | 22 | 7 |
| `app-post-url` | synth-app | COMPLETED, verified | 203,571 | $0.79 | 160 s | 23 | 6 |
| `lib-stopwords` | synth-lib | PARTIAL, `APPROVED_INPUTS_UNAVAILABLE` (as designed) | 24,720 | $0.07 | 54 s | 8 | 3 |
| `qualify-integration` | both | passed: synth-app `2eab103`, synth-lib `b84ab25` (the validated commits) | n/a (no child) | | | | |

- **Headline:** 217,276 tokens per verified task (434,551 over 2 verified tasks; the PARTIAL task's spend is included). Cost $1.66, a lower bound because the Codex reviewer is unpriced.
- **Earlier first-wave attempt** on the previous synth-lib pin (`wave1-first-attempt.json`): `lib-slugify` was verified at 159,139 tokens and $0.61.
- **Against #54:** `lib-slugify` is 159k and 206k here, against 131–192k there (n = 6). The spread is the Claude worker's cache writes: 99k, 157k and 149k here, against about 95–155k in #54. That is #55.
- **Lead:** 28–51k input and 2–3k output per task.

## What was observed (#4 checklist)

| #4 item | Result |
| --- | --- |
| Dependency gating | `app-post-url` refused with `WORKSPACE_TASK_BLOCKED` until `lib-slugify` was promoted; `promote` refused with `WORKSPACE_CI_NOT_GREEN` before CI |
| Second task in the same repository | `lib-stopwords` launched after `lib-slugify` with HEAD at the pin and no leftover worktree, but **on a dirty checkout** (see defects) |
| Budget spend and hold | 206,260 of 120,000 after wave 1: both wave-2 tasks `held_by_budget`, a direct launch refused with `WORKSPACE_BUDGET_EXHAUSTED`, no child started |
| Qualification at validated commits | passed in both repositories at the validated commits |
| Draft PRs linking upstream, nothing merges | [synth-lib#2](https://github.com/dhuzard/synth-lib/pull/2), [synth-app#2](https://github.com/dhuzard/synth-app/pull/2); the app PR names `dhuzard/synth-lib#2` and `slugify@1`; both draft, CI green, unmerged |
| Concurrent children, repository isolation | both wave-2 children started at 09:25:34 in separate process groups, one per repository |
| Resume after an interrupted child | the mechanism works, but the run does not complete (`interrupt-resume.json`, see defects) |

### Authorization (#64)

| Case | Result |
| --- | --- |
| Repositories missing, access check on | plan BLOCKED, `WORKSPACE_REPOSITORY_ACCESS_DENIED`; launch refused |
| Explicit identity absent from the remotes | `TARGET_REPOSITORY_CONFLICT` |
| Headless child under a mutating policy without `--allow-mutation` | `MUTATION_NOT_AUTHORIZED` |
| Issue write and comment before the default branch existed | refused, no write (`GITHUB_READ_FAILED`, see defects) |
| Issue create, issue comment, PR comment, with the checkout's own committed policy enabling them | `GITHUB_WRITE_NOT_AUTHORIZED` (dry run), no write |
| Draft PR publication | authorized by the default-branch policy (its commit is recorded), `Refs #1` |

### Interrupt and resume (`interrupt-resume.json`, local workspace, no GitHub)

- **Kill:** SIGKILL to the child's process group 60 s in. The execution was recorded `interrupted` (exit -9), the run stayed `IN_PROGRESS` (`needs_reconciliation`) and the lease was kept. No orphaned processes.
- **Plain `schedule`:** restarted nothing.
- **`schedule --resume-interrupted`:**
  - took the lease over (`HEADLESS_RESUME_TAKEOVER`, `OWNER_PROCESS_DEAD`);
  - verified the checkpoint;
  - continued the same run (launch 2).
- **The resumed lead stopped.** `orchestration_record status` answered `ROUTE_RECHECK_REQUIRED` ("BLOCKED: rerun route_task and complete backend preflight"). The lead read that as a stop and ended without an outcome, so the run finished BLOCKED (`ORCHESTRATION_OUTCOME_UNRECORDED`) and its phase and worker were reconciled to abandoned.

### Provenance

`benchmark_metrics --compare wave1-first-attempt.json full-run.json` reports COMPLETE: no `lib-slugify` fact of the first attempt is missing from the full run. No run left a phase, worker or wave open.

## Defects found

Fixed in #66 (kit):
- **The synth-lib CI workflow ran `test_stopwords`, which can never pass**, so `lib-slugify` could never be CI-green or promoted. The deterministic suite missed it because its fake GitHub reports whatever CI result a test sets. A kit test now runs each workflow's command on the reference solution.
- **`run_real.py` never passed `--allow-mutation`**, so the first attempt was refused, correctly.

Filed:
- #67: a closed draft PR makes its task unpublishable in that repository forever (`PUBLICATION_PR_CONFLICT`).

Proposed:
1. **A resumed lead stops at `ROUTE_RECHECK_REQUIRED`** instead of re-routing, so resume never completes in a real run. The recorder's message reads as a stop.
2. **The lead applied the integrated diff to the parent checkout** (`git diff … | git apply`), leaving it dirty. The restore and the launch gate check only HEAD, so the next task in that repository launched on a dirty checkout. The lead side relates to #63.
3. **`GITHUB_READ_FAILED: GitHub operation failed` hides the cause** (HTTP 409, empty repository) when the default-branch policy cannot be read.
4. **`tools/github_integration.py` run from a development checkout writes run records** into `provenance/runs/`, which is not gitignored.
5. **`pico workspace status` prints BLOCKED with `ok: true` and no blocker codes.**
6. **`pico workspace ci` reports `red` when Actions jobs never started** (account billing), the same as a failing test.
7. **The integration branch `pico/batch-1` is left behind** in each repository after a run.
8. **The lead runs on `gpt-6.1-sol`** (Pi's default) rather than an economy tier. Lead cost is $0.07–0.15 per task, so the Claude worker still dominates (#55).
