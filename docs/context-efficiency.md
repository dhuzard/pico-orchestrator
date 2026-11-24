# Context efficiency pilot

PICO's repeated lead prompt and Pi's parent context are separate sources of
token use. This pilot reduces the initial prompt and lets Pi compact the parent
earlier for one model in one target repository. Worker routing, permissions,
review, and publication remain governed by their existing contracts.

## Baseline and changes

The baseline is seven recent Metadatapp pilot runs with 14–21 lead calls each
(132 lead calls total).
Their recorded peak parent context was roughly 50–65k tokens, with zero Pi
compactions. Recorded lead cache reads were roughly 0.58–1.08 million tokens
per run. These are observed counters, not a bill or an estimate of tokens saved.
Together they recorded 6,416,341 lead input-volume tokens (fresh input,
cache reads, and cache writes); 94.6% of that volume was cache reads. The
median of run-level lead read-side medians was 46,802 tokens.
Three peaked above 55k; three completed, three were partial, and one
was blocked. That outcome mix makes a simple "per completed run" ratio
especially misleading.
The task mix, model cache behavior, and run duration vary, so these runs are
comparison examples rather than a controlled experiment.
The new report reproduced the lead figures from their real run records.
All-participant input volume was `n/a` because some worker counters were
missing; run records alone supplied no verified-task denominator.

`prompts/orchestrate-core.md`, which the launcher sends with each new run,
is reduced from 29,360 to 11,057 UTF-8 bytes. The full
`prompts/orchestrate.md` remains available for focused reads after routing.
A test bounds the core prompt to 12,000 bytes and checks that the key route,
mutation, review, integration, and publication instructions remain present.
The byte reduction is about 62%; actual input-token savings need a provider usage
comparison.

For the Metadatapp pilot, Pi's project `.pi/settings.json` sets
`compaction.modelOverrides["claude-bridge/claude-fable-5-1"]` to
`reserveTokens: 945000` and `keepRecentTokens: 12000`. With this model's
1,000,000-token context window, Pi's normal threshold becomes 55,000 tokens.
Pi performs and records compaction; PICO does not call `ctx.compact()` because
that API aborts the current turn. Other models keep their existing settings.

## What to carry over from the supplied workflow

Keep short, task-scoped lead sessions, durable run and mission records, focused
file reads, small worker contracts, model routing, independent review, and
checks tied to the exact commit. PICO already implements much of this through
fresh worker contexts, run provenance, routing, worktrees, and review steps.
Search for a symbol or file before reading large files, and bound tool output
to the lines needed for the current decision. Measure whether this changes
lead calls or review quality before adding a blocking read-size guard.

The supplied workflow's 60 KB read cap, 110/150/200k compaction thresholds,
model ranking, and subscription-point weights are observations from another
toolchain. They are not PICO defaults. PICO's 55k pilot threshold is specific
to the observed Fable runs and must be tested for continuity. PICO must not
call Pi's manual `ctx.compact()` inside a turn: that API aborts the turn.

## Measure token savings

Before a live run, verify that the installed Pi loads the project setting and
that `shouldCompact` is false at 54,999 tokens and true at 55,001. Run PICO's
normal checks after changing the prompt. These checks validate configuration
and invariants, not the quality of an actual model-generated summary.

The VM must actually run a Pico revision containing the shorter prompt, and
the target checkout used by Pi must contain `.pi/settings.json`, before an
"after" run tests either change. Record both exact revisions in the result.
The merged PRs alone do not activate them in an existing VM checkout.

Use `tools/benchmark_metrics.py` on explicit run IDs or workspace IDs. A
workspace report can qualify a completed task as verified from its checks;
a run-ID-only report cannot infer acceptance checks. For example:

```bash
STATE=/absolute/path/to/pi-coding-orchestrator-state
python3 tools/benchmark_metrics.py --state-dir "$STATE" --workspace BEFORE_ID --output /tmp/pico-before.json --json >/dev/null
python3 tools/benchmark_metrics.py --state-dir "$STATE" --workspace AFTER_ID --output /tmp/pico-after.json --json >/dev/null
python3 tools/benchmark_metrics.py --compare /tmp/pico-before.json /tmp/pico-after.json
```

The report keeps the existing **budget spend** metric (fresh input + output +
cache writes) separate from **input volume** (fresh input + cache reads +
cache writes). The latter is a count of provider-reported input tokens, not a
bill or network byte count. It reports lead and all-participant input volume,
volume per verified task, the median of each run's median lead read-side
input per call (fresh + cache read; per-call cache writes are not recorded),
Pi compactions, and peak context. Missing counters yield `n/a`, never zero.
Reported USD cost is usable only when cost coverage is complete; a Team-seat
usage window cannot be inferred from API list prices. The seven baseline runs'
lead aggregates matched the sums of their individual calls, so the baseline
does not need transcript-level de-duplication.

Run a smoke pair first, then several matched pairs with the same objective,
base commit, acceptance check, lead/worker models and effort, policy, and
tool bundle. Alternate arm order to reduce cache and time effects. To isolate
the two changes, compare the shorter prompt with compaction held fixed, then
compare default and 55k compaction with the prompt held fixed. A historical
before/after comparison with both changes combined is descriptive only.
The `benchmarks/efficiency/` harness supports paired launch-brief and core
prompt tests with guarded provider spending. Its analyzer reports paired input
volume, read-side median, and compaction deltas when observed. The core-prompt
mode holds the runtime and launch brief fixed and varies reviewed prompt
snapshots. The harness still needs a separate reviewed extension to vary Pi's
project compaction setting.

For every pair, inspect the run trace and checks for:

- completion and review findings, including any lost decision or task state;
- lead calls, fresh input, cached input, output, compactions, and peak context;
- resumed execution after each compaction and final publication state.

Compare input volume per verified task and the run median as well as totals.
A smaller context with more retries, missed findings, or failed checks is not
a saving. If compaction loses a required decision or increases retries,
remove the model override from the target's `.pi/settings.json` and rerun
from a clean session. Do not silently change the threshold during an active run.
