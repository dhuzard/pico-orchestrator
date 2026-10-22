# Synthetic orchestration benchmark

A small, reproducible workspace for measuring PICO orchestration, not model quality. It serves two issues:

- **#43**: the same task run on two PICO revisions with real backends (before and after), reporting tokens, calls, duration, cost, outcome and provenance completeness.
- **#49**: the same workspace is the main regression benchmark, in two separate parts: deterministic integration tests with fake backends that run in CI on every pull request, and real-agent benchmarks that are only ever started by hand (see "Two parts, kept apart" below).

Nothing here contains scientific content, and nothing is estimated: every metric comes from a recorded run, and an unobserved value is reported as unknown (`null` in JSON, `n/a` in tables).

## What `setup.py` builds

`setup.py TARGET_DIR` (or `setup.sh`, a one-line wrapper) creates, outside this repository, two throwaway Git repositories and `workspace.yaml`:

| Task | Repository | Depends on | Purpose |
| --- | --- | --- | --- |
| `lib-slugify` | `synth-lib` | none | Add `slugify(text)` with exact semantics. Provides contract `slugify@1`. Ends COMPLETED and verified. |
| `lib-stopwords` | `synth-lib` | `lib-slugify` | Needs `data/approved-stopwords.txt`, which does not exist. The objective forbids inventing it and asks for PARTIAL with `APPROVED_INPUTS_UNAVAILABLE`. |
| `app-post-url` | `synth-app` | `lib-slugify` | Add `post_url(title, slugify)`. Requires `slugify@1` from the other repository. |
| `qualify-integration` | `synth-app` | `lib-slugify`, `app-post-url` | Qualification task: checks both repositories at the validated commits, the app's whole suite in `synth-app` and the slugify tests in `synth-lib` (not its whole suite: `lib-stopwords` is meant to fail). No child run. |

Every implementation task has an explicit `objective`, one deterministic `checks` command (a pre-written `unittest` module that fails until the work is done and that the worker is told not to edit), and `max_duration_seconds` (900, 600, 900). The manifest has `budget.max_tokens: 5000000` by default (generous, so the benchmark measures instead of blocks; `--max-tokens N` sets a bounded budget), `max_parallel: 2` and a 1800 s default limit.

Expected verified result with a competent backend: 2 verified implementation tasks (`lib-slugify`, `app-post-url`), 1 PARTIAL (`lib-stopwords`, checks cannot pass), and the qualification task passing. `reference/` holds a known-good solution for each task; tests use it to prove the checks can pass, and #49's fake backends can replay it.

Repository identity comes from a remote URL such as `https://github.com/pico-bench-sandbox/synth-lib.git` and an explicit `repository` in the manifest, so `resolve_target` succeeds without contacting GitHub. Commits use a fixed author and date, so the pinned commits are identical on every run of `setup.py` (checked by a test): two revisions are measured against identical inputs. Use `--owner OWNER` to name a different owner.

## Identity and publication constraints

- **No GitHub access is needed to plan, launch the first wave, validate, and measure.** Pass `--no-access-check` to `pico workspace` (the documented flag that skips the `gh repo view` check, which would fail for repositories that do not exist). Publication is off: the repositories carry no `.pi/autonomy.yaml`, so `pico workspace publish --apply` is refused with `PUBLICATION_NOT_AUTHORIZED`.
- **Dependent tasks do not unblock locally.** The planner only unblocks a dependent when its dependency is `ready_for_downstream` or `merged`. `ready_for_downstream` requires a published draft PR whose head is the validated commit and CI observed green (`pico workspace promote`), which needs a real GitHub repository. Declaring `readiness: merged` in the manifest would assert something false and bypass that gate, so the kit does not. Without GitHub only `lib-slugify` can run (`WORKSPACE_TASK_BLOCKED` for the rest).
- **For the full four-task run, use real sandbox repositories.** Create two empty repositories you own named `synth-lib` and `synth-app`, one open issue in each, and run setup with `--owner YOUR_OWNER --publication --i-own-these-repos --issue lib-slugify=N --issue lib-stopwords=N --issue app-post-url=M`. Push each baseline (`git push -u origin main`) yourself, then publish each validated task with the reviewed dry run and `--apply`, wait for CI (the baseline includes a `unittest` workflow), and `pico workspace promote`. Draft PRs only; nothing merges. `--publication` with an owner other than the default is refused without `--i-own-these-repos`. Pushing the baseline and enabling `--publication` are your decisions; the kit never does either. `setup.py` also refuses any target inside a Git repository (and stops, rather than continuing, if Git cannot answer, for example on an ownership error) and builds commits independent of your Git configuration (no global or system config, no signing, no hooks).

## Run it on a pinned PICO revision

Use one fresh workspace and one state directory per revision. The same workspace id on one state directory would reuse execution records.

```bash
REV=<40-hex PICO commit>          # BEFORE, then repeat with AFTER
NAME=before                        # or: after
BENCH=$PWD/benchmarks/synthetic-workspace   # from a checkout of this kit
WORK=$HOME/pico-bench/$NAME

python3 "$BENCH/setup.py" "$WORK" --owner YOUR_OWNER    # add --publication --issue ... for the sandbox run
export PI_CODING_ORCHESTRATOR_REF=$REV                  # pin the runtime to the revision under test
export PI_CODING_ORCHESTRATOR_STATE_DIR=$HOME/pico-bench/state-$NAME

pico workspace status   "$WORK/workspace.yaml" --no-access-check      # plan and resolve identities
pico workspace schedule "$WORK/workspace.yaml" --no-access-check --json   # dispatches what is dispatchable; waves 2 and 3 need GitHub (see above)
# or one task at a time:
pico workspace launch   "$WORK/workspace.yaml" --task lib-slugify --no-access-check
pico workspace validate "$WORK/workspace.yaml" --task lib-slugify --head pico/<branch the run created>
```

Use the same `REV`-pinned runtime for every command of a run, and run the AFTER side with identical inputs, backends and model settings. `PI_CODING_ORCHESTRATOR_REF` is the documented way to pin the cached runtime for `pico workspace`; `pico pin` writes a project lock that the other launcher commands honor.

## Collect metrics

`tools/benchmark_metrics.py` is read-only and runs no model.

```bash
# One revision (the state dir is the one used above)
python3 tools/benchmark_metrics.py --state-dir "$PI_CODING_ORCHESTRATOR_STATE_DIR" --workspace pico-bench \
    --output "$HOME/pico-bench/$NAME.json"

# Before and after: tables, deltas and the provenance diff (exit code 1 if anything is missing from AFTER)
python3 tools/benchmark_metrics.py --compare "$HOME/pico-bench/before.json" "$HOME/pico-bench/after.json"
# or straight from the state directories
python3 tools/benchmark_metrics.py --compare "$HOME/pico-bench/state-before@pico-bench" "$HOME/pico-bench/state-after@pico-bench"
# run ids instead of a workspace: STATE_DIR@RUN_ID[,RUN_ID...] or: --state-dir STATE RUN_ID ...
```

Per run and in total it reports:

- tokens, kept separate (input, output, cache-read, cache-write), for the lead and for each worker backend;
- calls: lead calls (`context.lead_calls`), `orchestration_record` calls by action (counted from the lead's Pi session JSONL, found through the execution records or `--session-dir`; only calls after that run's lifecycle entry are counted when a session hosts several runs; `null` when no session is available), and worker calls by backend;
- wall-clock: per run, start to end; totals give `run_seconds_sum` (sum over runs) and `wall_clock_span_seconds` (first start to last end), which differ when runs overlap;
- cost: total, `cost_complete` and the unpriced participants;
- outcome: final status and whether each task's checks passed (from `pico workspace validate`; **run it for every completed task, or the task counts as unverified**);
- **headline: tokens per successfully completed and verified task** = (input + output + cache-write, lead and workers) divided by the tasks whose run is COMPLETED and whose checks all passed; `n/a` when there are none. If a participant did not report all three counters, the headline is flagged as a lower bound.

Provenance completeness (#43): `benchmark_metrics.provenance_diff(before_record, after_record)` and the `--compare` output list every phase, worker, wave, integrate and finish fact of BEFORE that is absent from, or differs in, AFTER, keyed by identity (phase id, phase/step/epoch, wave id), not by order. Runs are paired by task id. The list must be empty. Limits to know:

- Bind has no standalone persisted event in the run record. With a lead session, binds are the `bind_workspace` calls in it (task_id, step_id; a missing task_id means the sole routed task). Without one, they come from an optional `binds` list in the record or ClaudeWorker `started` activity. Every bind is keyed by (task, step) only; the evidence is recorded beside the fact and is not compared. Each run reports its `bind_source` (`session`, `record` or `activity`). If the two sides used different sources, `--compare` flags `bind_source_mismatch` (comparison not complete, exit 1) and does not diff binds. A `bind_workspace` call without task_id in a run with several phases cannot be attributed: it is listed under "Unresolved binds" (step and call order), not diffed.
- Interactions are excluded by default because #43 drops directive and feedback events; pass `interaction_types=("decision",)` to `provenance_facts` to require the lead's decisions.
- Phase ids are chosen by the lead. If two runs name the same task differently, the diff reports it; that is a real difference in the record.

The #43 failure and recovery modes (resume after an interruption, a worker failure, an integration conflict) are not produced by this happy-path workspace; inject them as separate runs on the same workspace (kill a child and `pico workspace launch --resume`, then record what the extension wrote) and measure them with the same tool.

## Two parts, kept apart (#49)

Stochastic model behavior and cache variance must not hide a change in scheduler correctness, and a CI run must not spend tokens. So the workspace serves two separate benchmarks:

| | Deterministic suite | Real-agent benchmark |
| --- | --- | --- |
| Where | `tests/test_synthetic_pilot.py` (harness: `tests/scenario_support.py`, `tests/scenario_lead.py`) | `run-real.sh REV LABEL` (`run_real.py`) |
| Runs | In CI on every pull request, and locally with `python -m pytest -q tests/test_synthetic_pilot.py` | Only when someone starts it with `PICO_REAL_BENCHMARK=1`. CI never sets that variable and never calls the script |
| Backends | A scripted fake `pi` and a fake GitHub. No model, no network, no tokens | The real `pi`, models and `pico` launcher |
| Proves | Orchestration is correct and safe: gates, budget, lease, limits, reconciliation, provenance | Task completion, model choice, prompt and context usage, run time and the cost the backends report |
| Result | Pass or fail, plus provenance compared with a committed golden record | A JSON report (`report.json`) with the metrics of `tools/benchmark_metrics.py` |

When the two disagree, the real run wins: add a scenario to the deterministic suite that reproduces what the real run showed.

### The deterministic suite

The real scheduler, launcher, run lease, recorder (`tools/parent_orchestration.py`, called through its CLI with the lease owner, as the extension does), contract gates (`validate`, `publish`, `ci`, `promote` against a fake GitHub backed by bare repositories on disk) and `benchmark_metrics` all run. Only the model is replaced: the fake `pi` of `tests/test_headless_run.py` takes the lease and starts or restores the run, then a scripted lead (`tests/scenario_lead.py`) replays a list of ops from a scenario file. Ops are data: record an event, apply the `reference/` solution on a `pico/<task>` branch, finish with a status, wait at a barrier, make N lead calls, hang, crash. A recorder call that is refused, or a task without a script, makes the child write `fake-scenario-error` to its log and the test fails: a scenario cannot pass by accident.

| Scenario (test) | What it asserts |
| --- | --- |
| `happy-multi-repo` | Wave 1 completes `lib-slugify`; it is validated, published as a draft PR, CI observed green and promoted. Only then do `app-post-url` (synth-app) and `lib-stopwords` (synth-lib) run, and they overlap in time (a barrier makes the test fail if they do not). The qualification task passes at the validated commits. Metrics: 2 verified tasks, headline 1050 tokens per verified task. |
| `dependency-gate` | `app-post-url` is refused with `WORKSPACE_TASK_BLOCKED` while `lib-slugify` has not run, is only implemented, only validated, only published, and while CI is pending, absent, or green on another commit (`WORKSPACE_CI_NOT_GREEN`). After promotion it is dispatchable. |
| `partial-and-reconcile` | `lib-stopwords` ends PARTIAL with `APPROVED_INPUTS_UNAVAILABLE` and nothing stays open: the running reviewer, the declared wave and a not-started phase are reconciled to `abandoned` (#42), and the blocked step keeps its blocker. Three restarts are refused with `PREREQUISITE_UNCHANGED` and counted once (#48). |
| `budget` | With `max_tokens: 2000`, a child that reported 1500 tokens and is still running makes the budget exhausted (2700, including in-flight spend). A direct launch is refused with `WORKSPACE_BUDGET_EXHAUSTED`, the scheduler holds the next task (`held_by_budget`), cost is a lower bound with the unpriced worker listed, and `--ignore-budget` is recorded. |
| `interrupt-resume` | A child killed with SIGKILL leaves a stale lease; nothing restarts it implicitly; resume without takeover is refused (`RUN_LEASE_STALE`); the scheduler's resume takes the lease over, the child verifies the checkpoint and finishes the same run. A second test moves the checkout (a branch switch at the same HEAD): the checkpoint is refused (`RESUME_CHECKPOINT_DRIFT`) and the run stays open. |
| `time-limit` | A hanging child is stopped at `max_duration_seconds` (900) on an injected clock: run BLOCKED with `EXECUTION_TIME_LIMIT`, nothing left open, the process group gone; relaunch and resume are no-ops (#46). |
| `lead-call-limit` | The recorder side of #47. The warning (call 3), the limit event (call 4) and the stop are scripted by the fake lead, which stands in for the extension (its enforcement is covered by `tests/orchestration-lifecycle.test.ts`), so those numbers are inputs. Asserted against production code: the launcher passes the limit and its source, the recorder stores the stop and leaves the run `IN_PROGRESS` with a fresh checkpoint and a released lease, the execution is interrupted with `stop_reason: LEAD_CALL_LIMIT`, a resume with the same limit is refused (`LEAD_CALL_LIMIT_EXHAUSTED`) by the launcher and the scheduler, and a raised limit continues the same run with the recorder keeping the earlier calls. |
| `retry-gate` | The same failure signature at the same HEAD and inputs exhausts the budget (1): `REPEATED_INEFFECTIVE_ACTION`, the step is blocked, the run ends PARTIAL, nothing open (#48). |

Each scenario also compares the provenance it left behind with `tests/fixtures/golden/<scenario>.json`: phase, worker, wave, bind, integrate and finish facts extracted by `benchmark_metrics.provenance_facts` (identities and statuses, never timestamps or ids), compared in both directions with `diff_facts` (identity, value and multiplicity), plus the task states, lead-call counts and tokens. A deliberate change of recorded provenance updates them in one step:

```bash
PICO_UPDATE_GOLDEN=1 python -m pytest -q tests/test_synthetic_pilot.py   # rewrites tests/fixtures/golden/*.json; review the diff
```

The variable is refused when `CI` is set, and CI never sets it.

What the deterministic suite does not show, and does not pretend to: whether a model records these events correctly (the scripts say what a competent lead and extension record), the TypeScript extension's own enforcement (covered by `tests/orchestration-lifecycle.test.ts`), real GitHub or CI behavior (the fake GitHub returns what the test sets), and that integrated waves are merged (the scripted lead records the wave events and leaves the work on `pico/<task>`; it does not merge). It takes about 40 seconds.

### The real-agent benchmark

```bash
PICO_REAL_BENCHMARK=1 benchmarks/synthetic-workspace/run-real.sh <40-hex PICO commit> <label> [--output-dir DIR] [--owner OWNER]
    [--max-tokens N] [--publication --i-own-these-repos --issue TASK=N ...] [--continue]
```

Without `PICO_REAL_BENCHMARK=1` (exactly that value) the script exits 2 and creates nothing. With it, it runs four steps on a pinned revision (`PI_CODING_ORCHESTRATOR_REF`) and one fresh workspace and state directory per label (default `~/pico-bench/LABEL`): setup, `pico workspace schedule`, `pico workspace validate` for every COMPLETED task, and `benchmark_metrics`. It writes `DIR/report.json`: the launch result, the validation results and the metrics report (tokens by participant, calls, models, duration, reported cost with the unpriced participants, outcome, and the headline tokens per verified task). It reports a missing `pico` or `pi` (exit 3) instead of faking one, and never pushes, opens a PR or merges. Without GitHub a default run covers the first wave; for all four tasks use sandbox repositories as described above: pass `--owner YOUR_OWNER --publication --i-own-these-repos` and the `--issue` numbers, so setup commits the sandbox policy and `pico workspace schedule` keeps its repository access check (a missing or inaccessible repository fails closed instead of being skipped). Push the baselines, publish and promote by hand, then run the script again with `--continue`, which reuses the options the first run recorded in `run.json`. To compare two revisions, give the `--compare` of `tools/benchmark_metrics.py` the two reports.

`tests/test_real_benchmark_entry.py` checks the boundary itself (the refusals, that CI never mentions the variable or the script) and the plumbing, with a fake `pi` and a `pico` stand-in. It sets the variable only in the environment of the child process under test.
