# Model and runtime configuration

The orchestrator separates **routing policy** from **concrete model selection**.

Routing policy decides which family, role, capacity tier, review topology, and
mutation permissions a task requires. User-owned model configuration decides
which concrete Claude or Codex model implements an existing
`light`/`economy`/`standard`/`high`/`max` tier.

This separation is intentional: a target repository may raise routing risk
floors through `.pi/routing.yaml`, but it cannot silently replace the models
used for high-risk work.

## Inspect the effective configuration

```sh
pico models show
pico models validate
pico doctor .
```

`pico models show` reports the effective concrete model for every family and
capacity tier, its assessed ambiguity capacity, and the Claude Code model-ID/minimum-version mapping used by
backend preflight.

See [Task ambiguity and model capacity](ambiguity.md) for the repository-based
classification rubric and matching behavior.

`pico doctor` validates the effective model configuration and also reports the
installed Pi and Claude Code versions/authentication together with the
orchestrator revision selected by the launcher.

## Select a Codex model for a capacity tier

```sh
pico models set codex light openai-codex/gpt-6-luna --ambiguity-capacity 3
pico models set codex economy openai-codex/gpt-6-luna --ambiguity-capacity 3
pico models set codex standard openai-codex/gpt-6-luna --ambiguity-capacity 6
pico models set codex high openai-codex/gpt-6.1-sol --ambiguity-capacity 8
pico models set codex max openai-codex/gpt-6-astra --ambiguity-capacity 10
```

A later supported Codex model can be substituted without editing
`routing/default-policy.yaml`:

```sh
pico models set codex high openai-codex/<new-model-id> --ambiguity-capacity 8
```

The route and assigned capacity tier are unchanged; only the concrete model
binding changes.

## Select a Claude model for a capacity tier

Known Claude policy models reuse the repository's existing Claude Code mapping.
A new Claude model must explicitly declare the backend model identifier expected
by Claude Code:

```sh
pico models set claude high anthropic/<new-policy-model> \
  --ambiguity-capacity 9 \
  --claude-code-id <claude-code-model-id>
```

When a model requires a minimum Claude Code release, record that requirement as
well:

```sh
pico models set claude high anthropic/<new-policy-model> \
  --ambiguity-capacity 9 \
  --claude-code-id <claude-code-model-id> \
  --min-version 3.0.0
```

Backend preflight fails closed if a selected Claude model has no explicit
Claude Code model-ID mapping, if the installed Claude Code version is below the
configured minimum, if authentication is unavailable, or if the executable
identity changes after launch.

## Different models for different tasks

Do not select an arbitrary model inside an individual target repository or task.
Instead bind concrete models to the five capacity tiers (policy v7), ordered
`light < economy < standard < high < max`:

| Tier | How a step reaches it | Codex default | Claude default |
|---|---|---|---|
| `light` | `ops-light` downgrade of routine operational/docs work (low effort) | `openai-codex/gpt-6-luna` | `haiku` |
| `economy` | `low-risk-worker` downgrade (low effort) | `openai-codex/gpt-6-luna` | `haiku` |
| `standard` | authored, or `bounded-implementation-worker`/`bounded-specifier` downgrade of a high step (medium effort) | `openai-codex/gpt-6-luna` | `sonnet` |
| `high` | authored | `openai-codex/gpt-6.1-sol` | `anthropic/claude-opus-5-5` |
| `max` | `critical-escalation` of the specifier and reviewer only, when `failure_cost=high`, `semantic_correctness=critical` and `ambiguity>=7` | `openai-codex/gpt-6-astra` | `anthropic/claude-opus-5-5` |

The default ambiguity capacities are numeric: Codex `3/3/6/8/10` and Claude
`3/3/6/9/9` across `light/economy/standard/high/max`. These are provisional
policy assessments. `pico models show` displays the effective values.

Execution plans author only `standard` and `high`; `light`, `economy` and `max`
are only ever assigned after routing. For Codex, `light` and `economy` use the
same model and differ only in the reasoning effort the rules assign.

**Pricing rationale (cost-first defaults).** Per 1M input/output tokens:
`gpt-6-luna` $0.10/$0.50, `gpt-6.1-sol` $2/$10, `gpt-6-astra` $10/$50. Luna is
the default for every routine Codex tier, Sol is reserved for authored
high-capacity specification and independent review, and Astra (5x Sol) is used
only for the narrow critical, ambiguous, high-cost escalation. Bounded work in
`CODEX_CLAUDE_CODEX` runs its Claude worker on `sonnet` instead of Opus, while
the independent reviewer stays on the authored high tier.

The deterministic classifier/routing policy decides which route and capacity a
task receives. That means different tasks can use different configured models
without allowing a repository or worker to weaken the routing decision.

## Reset overrides

Reset one binding:

```sh
pico models reset codex high
```

Reset all user overrides and return to repository defaults:

```sh
pico models reset --all
```

## Configuration ownership

The default user configuration path is:

```text
~/.config/pi-coding-orchestrator/models.yaml
```

When `XDG_CONFIG_HOME` is set, it is used instead of `~/.config`.

For an explicit configuration, set an **absolute** path:

```sh
export PI_ORCHESTRATOR_MODEL_CONFIG="$HOME/.config/pi-coding-orchestrator/models-lab.yaml"
pico models show
pico run .
```

Relative paths are rejected so changing into a target repository cannot redirect
model configuration to repository-controlled content.

The model-runtime schema permits concrete model bindings, declared ambiguity
capacities, and Claude Code compatibility metadata. It cannot define routes, precedence, risk floors,
capacity-assignment predicates, execution topology, mutation permissions, or
review requirements.

## Updating runtimes

`pico update` updates the cached **pi-coding-orchestrator** checkout while
preserving generated provenance and refusing a dirty/unsafe cache.

Pi and Claude Code are external runtimes. Their installation mechanism can vary,
so the orchestrator deliberately does not run a package manager or silently
upgrade them. Use `pico doctor .` to inspect the installed versions and backend
compatibility, then upgrade the external runtime with the same mechanism by
which it was installed. Re-run `pico doctor .` afterward.

This keeps runtime upgrades explicit and prevents an orchestrator update from
unexpectedly changing globally installed developer tooling.
