# Synthetic orchestration benchmark

A small, reproducible workspace for measuring PICO orchestration, not model quality. It serves two issues:

- **#43**: the same task run on two PICO revisions with real backends (before and after), reporting tokens, calls, duration, cost, outcome and provenance completeness.
- **#49**: the same workspace later runs in CI with fake backends as a regression test for orchestration invariants.

Nothing here contains scientific content, and nothing is estimated: every metric comes from a recorded run, and an unobserved value is reported as unknown (`null` in JSON, `n/a` in tables).

## What `setup.py` builds

`setup.py TARGET_DIR` (or `setup.sh`, a one-line wrapper) creates, outside this repository, two throwaway Git repositories and `workspace.yaml`:

| Task | Repository | Depends on | Purpose |
| --- | --- | --- | --- |
| `lib-slugify` | `synth-lib` | none | Add `slugify(text)` with exact semantics. Provides contract `slugify@1`. Ends COMPLETED and verified. |
| `lib-stopwords` | `synth-lib` | `lib-slugify` | Needs `data/approved-stopwords.txt`, which does not exist. The objective forbids inventing it and asks for PARTIAL with `APPROVED_INPUTS_UNAVAILABLE`. |
| `app-post-url` | `synth-app` | `lib-slugify` | Add `post_url(title, slugify)`. Requires `slugify@1` from the other repository. |
| `qualify-integration` | `synth-app` | `lib-slugify`, `app-post-url` | Qualification task: runs the app's whole suite at the validated commits. No child run. |

Every implementation task has an explicit `objective`, one deterministic `checks` command (a pre-written `unittest` module that fails until the work is done and that the worker is told not to edit), and `max_duration_seconds` (900, 600, 900). The manifest has `budget.max_tokens: 5000000` (generous, so the benchmark measures instead of blocks), `max_parallel: 2` and a 1800 s default limit.

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

## Turning it into the #49 CI test

1. `setup.py` already makes everything deterministic: fixed commits and pins, no network, fixed task ids.
2. Replace the backends with scripted ones. `tests/test_headless_run.py` has the fake `pi` (`FAKE_PI`, with `FAKE_PI_BEHAVIOR` and `FAKE_PI_USAGE`) and `tests/test_workspace_contracts.py` has a fake GitHub adapter that makes `validate`, `publish`, `ci` and `promote` work offline. A scenario script applies `reference/<repo>/...` for the tasks that should complete and writes nothing for `lib-stopwords`, so it ends PARTIAL.
3. Assert invariants against the state directory with `benchmark_metrics`: headline and verified counts, `provenance_diff` empty against a stored reference record, no phase or worker left `in_progress` after the PARTIAL run (#42), budget holding with in-flight spend (shrink `budget.max_tokens` in a copy of the manifest, #44/#45), `EXECUTION_TIME_LIMIT` (lower `max_duration_seconds` with a `slow` fake, #46), lead-call and repeated-action limits (#47, #48), and the run lease on resume (#40).
4. Where the fake and a real run disagree, the real run wins: record its metrics (`--output`) as the reference and add a fake scenario that reproduces it.

`tests/test_benchmark_metrics.py` already covers the metrics tool on fixture records, `setup.py` (planner validates it, checks fail before and pass with the reference solution), and the plumbing of launching `lib-slugify` with the fake `pi` and measuring it.
