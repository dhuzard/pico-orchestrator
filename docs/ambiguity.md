# Task ambiguity and model capacity

`ambiguity` is residual uncertainty in the intended result **after** reading the
relevant repository context. It is an integer from **0 to 10**. Scope,
implementation difficulty, failure cost, and an unknown bug cause are separate
fields. The score is a routing aid, not a measurement of model intelligence.

## Score the task

Inspect the request, acceptance criteria, nearby code and tests, interfaces,
repository instructions, and design decisions. Record unresolved choices in
`ambiguity_assessment` under `outcome`, `contract`, `design`, `evidence`, and
`authority`. Each dimension has a numeric `level`, `unresolved_choices`, and
`repository_evidence`. The task's `ambiguity` is the **maximum** dimension score
before any project risk floor. A dimension scored 4 or higher must name a
choice and a specific observation, including where inspection found no answer.

| Score | Anchor | Example |
|---|---|---|
| 0–1 | Result and contract are explicit. | Apply a specified config value. |
| 2–3 | A small local choice is settled by a nearby pattern. | Follow an existing API test pattern. |
| 4–6 | Bounded choices remain; conventions or tests constrain them. | Choose a component layout within specified behavior. |
| 7–8 | Consequential behavior or contract alternatives remain. | Two interfaces disagree about ownership of persisted data. |
| 9–10 | Severe conflicting or absent contract; likely needs decomposition or an owner decision. | A migration changes data meaning and no source establishes the intended semantics. |

The five dimensions ask different questions:

| Dimension | Question |
|---|---|
| `outcome` | Is the desired user-visible behavior clear? |
| `contract` | Do interfaces, data rules, and acceptance criteria agree? |
| `design` | Are several materially different implementations plausible? |
| `evidence` | Does inspected repository evidence resolve the choice? |
| `authority` | Can a worker decide, or must the user or another owner decide? |

An `authority` score of 7 or above means the owner must decide before dependent
changes. No model capacity score grants authority. A missing investigation alone
does not justify a high ambiguity score: inspect first. For scores 7–10, also
provide `escalation_evidence.ambiguity` naming the choice and repo evidence.
The structured record is optional for older task inputs. For new scores of 4 or
more, the captain should provide it; a routine 0–3 task needs no five-axis
worksheet unless the choice is disputed.

Specification or discovery may reduce the residual score for a *new* dependent
task. The original task remains conservatively classified; the captain can
split investigation, specification, implementation, and review into separately
routed tasks.

## Match configured workers

Each configured family and capacity tier declares an operator-assessed numeric
`ambiguity_capacity`. Defaults are provisional policy values, **not** measured
success thresholds:

| Tier | Codex model / capacity | Claude model / capacity |
|---|---|---|
| `light` | Luna / 3 | Haiku alias / 3 |
| `economy` | Luna / 3 | Haiku alias / 3 |
| `standard` | Luna / 6 | Sonnet alias / 6 |
| `high` | Sol 6.1 / 8 | Opus 5.5 / 9 |
| `max` | Astra / 10 | Opus 5.5 / 9 |

The same binding has the same score in light and economy, and Claude high and
max share the same Opus capacity. Runtime aliases must be resolved before
claiming an exact model identity; a local `haiku` probe previously executed
Sonnet 5.5.

Capacity assignment skips a downward rule when the candidate model score is
below the effective task score. If the authored step is also insufficient,
routing fails closed. A score above a required worker's capacity calls for
clarification, decomposition, or a reviewed policy/model change; the captain
must not silently substitute a different route. The existing high-ambiguity
topology begins at score 7: Codex specifies, Claude implements, and a fresh
Codex context reviews. Critical, high-cost work can raise the specifier and
reviewer to max; it does not raise the implementation worker.

`pico models show` displays effective capacities. New bindings require a
declared score that meets the tier floor:

```sh
pico models set codex high openai-codex/<model-id> --ambiguity-capacity 8
pico models set claude standard anthropic/<model-id> \
  --ambiguity-capacity 6 --claude-code-id <backend-model-id>
```

Legacy string overrides for unfamiliar models receive capacity 0 and cannot
pass higher-tier validation. Matching remains family neutral, but only Codex
and Claude have adapters today. The router compares the overall task score;
per-dimension model ratings remain research hypotheses. See
[model ambiguity evaluation](model-ambiguity-evaluation.md) and
[work modes](work-modes.md).

For newly routed runs, `pico watch` shows each worker's route, task score,
selected tier, declared model capacity, applicable tier rule, and recorded
dimension/work scores. Older runs have no saved routing summary. The viewer
stores only policy metadata and numeric scores, not task prose or repository
evidence.

## Workflow examples

These examples use the current policy and default bindings. The captain first
identifies `task_type`, work modes, risk, and ambiguity from repository evidence;
`route_task` then chooses the plan.

| Task after inspection | Score and work demand | Current decision |
|---|---|---|
| Apply a specified small config value | Ambiguity 2; little search, analysis, or verification | Claude light (Haiku alias, capacity 3) under `ops-light`. |
| Implement an agreed cross-layer feature | Ambiguity 5; search 5, implementation 6, verification 5 | Claude standard (Sonnet alias, capacity 6), then independent Codex review. |
| Resolve disputed data semantics and implement a high-cost fix | Ambiguity 9; analysis 8, implementation 7, verification 8 | Codex max (Astra, capacity 10) specifies; Claude high (Opus, capacity 9) implements; a fresh Astra context reviews. |
| Same task with ambiguity 10 | Severe unresolved contract | Current plan has no compatible Claude implementation worker; routing stops for clarification, decomposition, or a reviewed policy change. |

If the owner must decide (`authority>=7`), a mutating task is rejected with
`AUTHORITY_DECISION_REQUIRED`. The captain can route read-only investigation,
then obtain the decision and classify a new implementation task.

## Complexity check

The 0–10 values add useful boundaries at 3/4, 6/7, 8/9, and 9/10. They
also invite false precision: no benchmark yet proves that a score of 5 is
meaningfully different from 6, or that a model with a declared capacity of 8
succeeds reliably at level 8. The policy still uses three broad route bands
(0–3, 4–6, 7–10); exact values only guard worker compatibility. For simple
tasks, record one score and task type. Use the dimension and work-mode profiles
when they change decomposition, authority, or worker choice. Recalibrate these
numeric capacities from observed repository tasks before treating adjacent
scores as real capability differences.
