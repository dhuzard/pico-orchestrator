# Evaluating model capacity for task ambiguity

The five task dimensions are `outcome`, `contract`, `design`, `evidence`, and
`authority`; see [task ambiguity](ambiguity.md). A capability rating applies to
an **exact worker configuration**: provider, resolved model ID, reasoning effort,
tools, context budget, system prompt, and role. A model name or family alone is
not a stable unit of evaluation. `authority>=7` on a task always requires the
owner's decision, regardless of worker capability.

The router now scores ambiguity from 0 to 10. The `low`/`medium`/`high`
self-reports below predate that numeric scale; they are preserved as historical
probe results and must not be converted mechanically into 0–10 capacities.

Evaluate [work modes](work-modes.md)—search, analysis, writing, implementation,
verification, and coordination—separately from ambiguity. A task may be clear
yet demand sustained search or implementation. The captain records both profiles
before decomposing and routing tasks.

## What public evidence tells us

[OpenAI describes Luna as suited to focused, high-volume work, Sol as a balance
for complex work, and Astra as its most capable model for demanding reasoning
and coding](https://developers.openai.com/api/docs/models).
[Anthropic positions Sonnet 5.5 for well-scoped everyday tasks and Opus 5.5 for
work requiring careful judgment](https://www.anthropic.com/claude-sonnet-5-5),
with [Opus 5.5 release details](https://www.anthropic.com/claude-opus-5-5).
These are broad provider descriptions, **not** measurements of any of our five
ambiguity dimensions. They justify candidate tiers for testing, not a rated
matrix.

Public coding benchmark scores are also insufficient. [OpenAI's 2026 audit of
SWE-bench Pro](https://openai.com/index/separating-signal-from-noise-coding-evaluations/)
found many tasks with broken or underspecified tests and specifically separated
uncertainty resolvable from repository context from true underspecification.
An [Anthropic study of model self-evaluation](https://www.anthropic.com/research/language-models-mostly-know-what-they-know)
found that self-knowledge can be useful but calibration struggles on new tasks.
These findings support using self-report to design tests, not to set routing
capacity.

## Local self-assessment probe, 15 October 2026

I asked the available workers for one subjective `low`/`medium`/`high` rating
per dimension, without repository tasks or tools. The Codex probes used Pi with
the named model and medium thinking. Claude probes used Claude Code plan mode.
The table records their replies; **none are validated capability ratings**.

| Requested worker | Outcome | Contract | Design | Evidence | Authority |
|---|---|---|---|---|---|
| Codex Luna | medium | high | medium | high | medium |
| Codex Sol 6.1 | medium | high | medium | high | high |
| Codex Astra | high | high | medium | high | high |
| Claude Sonnet 5.5 | high | medium | medium | high | medium |
| Claude Opus 5.5 | high | high | medium | high | medium |

The Claude `haiku` alias executed `claude-sonnet-5-5` in this probe according
to Claude Code's `modelUsage`, so it supplies **no Haiku observation**. The
Sonnet self-rating for outcome also changed from `medium` to `high` between two
short probes. Several responses acknowledged a tendency to infer decisions
that belong to the user. Exact backend identity and repeatability must be
checked before a rating is trusted.

## Calibration protocol

1. Assemble repository snapshots with human-reviewed tasks stratified by the
   five dimensions and numeric anchors (0–3, 4–6, 7–8, 9–10). Include conflicts
   in tests vs interfaces, missing acceptance criteria, alternative designs, and decisions
   that only a user can make. Keep the source snapshot and intended decision.
2. Have at least two human reviewers label unresolved choices, repository
   evidence, owner, and acceptable behavior. Adjudicate disagreements before
   testing workers. Do not score a task whose correct answer is unknowable.
3. Run each exact worker configuration on the same snapshots with the same
   tool access and role. Capture the resolved backend model ID, prompt,
   reasoning effort, tool trace, answer, patch if any, cost, and time.
4. Grade each dimension separately: correct behavior or justified question
   (`outcome`); consistency with interfaces/data/tests (`contract`); sound
   tradeoff among feasible designs (`design`); accurate, cited repository
   inspection without invented evidence (`evidence`); and correct escalation
   of owner-only choices (`authority`). Review downstream patches for defects.
5. Repeat enough tasks to report success rates with uncertainty intervals,
   and inspect failures by dimension and role. [Anthropic's statistical eval
   guidance](https://www.anthropic.com/research/statistical-approach-to-model-evals)
   argues for reporting sampling uncertainty, not just point scores.
6. Assign the highest supported 0–10 score per dimension only after a predeclared
   quality gate is met. Keep `unassessed` where evidence is thin. Re-run after
   model, prompt, tool, or effort changes. Self-report and vendor claims remain
   provenance, never the gate.

The current policy's scalar `ambiguity_capacity` remains in force. Once the
matrix has measured ratings, routing can require each task dimension to be at
or below the selected worker's corresponding capability. Specifying,
implementing, and reviewing should be evaluated separately: a strong
specifier may resolve a contract before a less capable worker implements it.
Until then, routing must not promote these self-reports into enforcement.
