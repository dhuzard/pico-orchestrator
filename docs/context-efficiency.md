# Context efficiency pilot

PICO's repeated lead prompt and Pi's parent context are separate sources of
token use. This pilot reduces the initial prompt and lets Pi compact the parent
earlier for one model in one target repository. Worker routing, permissions,
review, and publication remain governed by their existing contracts.

## Baseline and changes

The baseline is seven recent Metadatapp pilot runs with 14–21 lead calls each.
Their recorded peak parent context was roughly 50–65k tokens, with zero Pi
compactions. Recorded lead cache reads were roughly 0.58–1.08 million tokens
per run. These are observed counters, not a bill or an estimate of tokens saved.
The task mix, model cache behavior, and run duration vary, so these runs are
comparison examples rather than a controlled experiment.

`prompts/orchestrate-core.md`, which the launcher sends with each new run,
is reduced from 29,360 to 10,671 UTF-8 bytes. The full
`prompts/orchestrate.md` remains available for focused reads after routing.
A test bounds the core prompt to 12,000 bytes and checks that the key route,
mutation, review, integration, and publication instructions remain present.
The byte reduction is 64%; actual input-token savings need a provider usage
comparison.

For the Metadatapp pilot, Pi's project `.pi/settings.json` sets
`compaction.modelOverrides["claude-bridge/claude-fable-5-1"]` to
`reserveTokens: 945000` and `keepRecentTokens: 12000`. With this model's
1,000,000-token context window, Pi's normal threshold becomes 55,000 tokens.
Pi performs and records compaction; PICO does not call `ctx.compact()` because
that API aborts the current turn. Other models keep their existing settings.

## Validation and rollout

Before a live run, verify that the installed Pi loads the project setting and
that `shouldCompact` is false at 54,999 tokens and true at 55,001. Run PICO's
normal checks after changing the prompt. These checks validate configuration
and invariants, not the quality of an actual model-generated summary.

Treat the first runs with the project setting as a pilot. For comparable task
types, inspect `python3 tools/backlog.py usage --json` and the run trace for:

- completion and review findings, including any lost decision or task state;
- lead calls, fresh input, cached input, output, compactions, and peak context;
- resumed execution after each compaction and final publication state.

Compare rates per completed task or lead call as well as totals. Cache reads
alone are not cost or quality. If compaction loses a required decision or
increases retries, remove the model override from the target's
`.pi/settings.json` and rerun from a clean session. Do not silently change
the threshold during an active run.
