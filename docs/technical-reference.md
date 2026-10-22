# Pi Coding Orchestrator

Reusable Claude + Codex routing policy for Pi.

Core routing principle:

- Claude maximizes completion under bounded ambiguity.
- Codex minimizes unjustified assumptions under consequential uncertainty.

Only four routes exist:

- CODEX
- CLAUDE
- CLAUDE_CODEX (Claude implements, fresh Codex reviews)
- CODEX_CLAUDE_CODEX (Codex specifies, Claude implements, a different fresh Codex reviews)

## Architecture and sources of truth

Each file owns exactly one concern. Nothing else is authoritative for that
concern:

| File | Sole source of truth for |
|---|---|
| `schemas/task.schema.json` | Task fields, their types/enums, and which fields are required. |
| `routing/default-policy.yaml` | Route match predicates, precedence, fallback route, schema-valid execution plans, post-route capacity assignment, execution backends, and centralized model profiles. |
| `routing/delegation-policy.yaml` | Nested lead-agent delegation policy: DIRECT/DELEGATE/PARALLEL/BLOCKED/SEQUENTIAL choice, fan-out limits, family biases, shared-state safeguards, retry behavior, metrics, and offline learning boundary. It never changes the selected top-level route. |
| `schemas/delegation-policy.schema.json` | Closed schema for the nested delegation policy, cross-validated against the centralized Claude capacity profile. |
| `schemas/execution-plan.schema.json` | The v3 authored-plan shape: ordered steps with role/family/intent plus `capacity_tier`, `reasoning_effort`, `context_strategy`, isolation, permissions, and dependencies. Plans never contain a tool or model name. |
| `schemas/project-routing.schema.json` | The shape of allowed project-local constraints (`.pi/routing.yaml`). |
| `tools/routing_engine.py` | The single implementation of the evaluator: safe YAML loading, schema validation, match-expression evaluation, precedence/fallback selection, risk-floor overrides, the no-downgrade override algorithm, execution-plan schema/semantic validation, primary-intent derivation, plan materialization, post-route capacity assignment, and backend-binding resolution. Imported by the static validator and the runtime adapter -- never reimplemented. |
| `prompts/orchestrate.md` | The operational protocol an orchestrating agent follows. It mirrors (never redefines) the schema's required field list and the policy's routes/fallback/review categories, executes a returned `execution_plan` generically via `backend_bindings`, and instructs the agent to call the `route_task` tool rather than select routes or interpret backend/model names itself. |
| `tests/routing-cases.yaml` | Fixtures for the deterministic evaluator, consumed by both the static validator and the runtime tests. |
| `templates/project-routing.yaml` | A safe, constraints-only example for a new project's `.pi/routing.yaml`. |

`prompts/orchestrate.md` references `routing/default-policy.yaml`,
`routing/delegation-policy.yaml`, and their schemas as paths relative to this
repository's root — it contains no environment-specific absolute filesystem
paths, so it works regardless of where this repository is cloned.

## Routing semantics

Each route in `routing/default-policy.yaml` defines a `match` expression: a
tree of structured nodes, each exactly one of:

- `{all: [expr, ...]}` — true iff every child expression is true (non-empty list required)
- `{any: [expr, ...]}` — true iff at least one child expression is true (non-empty list required)
- `{field, operator, value}` — a leaf predicate over a task field

Operators are exactly `==`, `!=`, and `IN`:

- `==` is type-sensitive JSON equality (`!=` is its negation)
- `IN` requires a non-empty array `value` and is true if the field equals any member

Every `field` referenced must exist in `schemas/task.schema.json`, and every
predicate `value` must be type/enum-compatible with that field.

All four routes are evaluated independently against a classified task:

- If exactly one route matches, use it.
- If more than one route matches, use the earliest route in `precedence`
  (`CODEX_CLAUDE_CODEX`, `CLAUDE_CODEX`, `CODEX`, `CLAUDE`, in that order).
  YAML map ordering elsewhere is never relied upon; `precedence` is the sole
  tie-breaker.
- If no route matches, use `fallback_route` (`CODEX`).

### Classification rubric and escalation evidence

`schemas/task.schema.json` carries the classification rubric as a
`description` on every field (the same schema is the `route_task` tool's
parameter schema, so the classifier sees it). Each risk flag defaults to its
low-risk value; repository-wide AGENTS.md guardrails govern worker conduct,
not task risk. Escalating values must be justified per task in the optional
`escalation_evidence` map (field name -> one non-empty line):

| Field | Escalating value |
|---|---|
| `failure_cost` | `high` |
| `semantic_correctness` | `critical` |
| `ambiguity` | `>=7` |
| `behavior_preservation` | `required` |
| `assumptions_must_be_explicit` | `true` |
| `security_sensitive` | `true` |
| `destructive_operation` | `true` |

`tools/route_task.py` checks the classification as submitted (before project
risk floors, so floor-raised values are exempt) and otherwise returns
`{"ok": false, "blocker_code": "CLASSIFICATION_EVIDENCE_MISSING",
"missing_evidence_fields": [...], "errors": [...]}`. The single list lives in
`tools/routing_engine.py` `ESCALATING_CLASSIFICATION_VALUES`.

Operational task types `vcs_operation`, `issue_management`, `config_edit` and
`ci_fix` are matched by the `CLAUDE` route's `task_type` list. They never
satisfy a Codex-only predicate by type alone; a `ci_fix` with an unknown cause
sets `diagnosis_required` and is taken by `CODEX` through precedence.

## Execution plans

Route selection (above) decides *which* of the four routes applies. A
separate, later step -- execution *materialization* -- decides the concrete,
machine-validatable plan an orchestrating agent must follow. Both are
computed by `tools/routing_engine.py`, and a successful `route_task` result
always contains both.

Each route's execution plan is a fixed topology of 1-3 steps in
`routing/default-policy.yaml`, one schema-valid variant per **primary
intent** (`analysis`, `implementation`, `review`):

- **CODEX** — one Codex `worker` step.
- **CLAUDE** — one Claude `worker` step.
- **CLAUDE_CODEX** — a Claude `worker` step, then a fresh, independent Codex
  `reviewer` step that depends on it.
- **CODEX_CLAUDE_CODEX** — a fresh Codex `specifier` step, then a Claude
  `worker` step that depends on it, then a *different* fresh Codex
  `reviewer` step that independently depends on both.

Only the `worker` step's `intent` and `mutation_permission` vary across the
three primary-intent variants of a route's plan; the topology, step ids,
families, and dependencies never change. The primary intent itself is
derived centrally, once, from the task's effective classification (never
duplicated in the prompt or the extension):

```
implementation_required == true          -> implementation
else task_type in {review, security_review} -> review
else                                      -> analysis
```

Every plan conforms to `schemas/execution-plan.schema.json`
(`additionalProperties: false` throughout) and these semantic rules,
enforced by `tools/routing_engine.py` and never reimplemented elsewhere:

- Step `id`s are unique; array position and the `order` field are both
  contiguous `1..N`.
- Every `depends_on` id refers to an earlier step (strictly smaller `order`)
  in the same plan.
- A `specifier` step always has `intent: analysis` and
  `mutation_permission: forbidden`.
- A `reviewer` step always has `intent: review` and
  `mutation_permission: forbidden`.
- A `worker` step's `mutation_permission` is `allowed` iff its `intent` is
  `implementation`; otherwise it is `forbidden`.
- `independent_review: true` implies `role: reviewer`, `intent: review`,
  `fresh_context: true`, `mutation_permission: forbidden`, and at least one
  dependency (an independent reviewer always reviews prior work).
- `independent_review_required` at the plan's top level is true iff some
  step has `independent_review: true`.
- Every authored top-level step's `fresh_context` is `true`. Continuing an
  already-spawned agent can never satisfy another authored fresh step or
  `independent_review`. Inside one active lead step, however, the validated
  delegation policy may prefer continuing a nested worker when follow-up
  depends heavily on its accumulated context.

Execution-plan schema version 3 requires three execution fields on every step:

- authored `capacity_tier`: one of the five ordered tiers
  `light < economy < standard < high < max` in the schema; policy plans may
  author only `standard`, `high` or `max` (the default policy authors `max`
  nowhere), while `light`/`economy` are selected only by validated downward
  post-route assignment and `max` only by the `critical-escalation` rule;
- `reasoning_effort`: `low`, `medium`, or `high`;
- `context_strategy`: `focused` (task/relevant artifacts only) or `full`
  (broader project context needed for end-to-end coherence), always subject to
  the no-secret/no-transcript context discipline.

These are execution metadata selected only after route selection; they do not
participate in route matching, precedence, fallback, primary-intent derivation,
or risk-floor handling. For a given route and step id the authored values are
identical across all three primary-intent variants.

After the route and primary-intent plan are fixed, `capacity_assignment.rules`
(policy v7) is evaluated against the **effective** classification. Rules are
ordered; for each authored step, a rule is a *candidate* when its
`step_match` (`role`, `intent`, `capacity_tier`, `independent_review`, and an
optional `family`, each a value or list) selects the step, and the first
candidate whose `match` guard fully holds wins. Otherwise the authored tier is
kept. The required rules, in policy order:

| Rule | Direction | Selects | Guard (all of) | Assigns |
|---|---|---|---|---|
| `ops-light` | down | `worker`, authored `standard`/`high`, any family | `task_type IN [vcs_operation, issue_management, config_edit, documentation, ci_fix]`, `failure_cost IN [low, medium]`, `implementation_scope==small`, `ambiguity<=6`, `semantic_correctness!=critical`, `diagnosis_required==false`, safety leaves | `light`, effort `low` |
| `low-risk-worker` | down | `worker`, authored `standard` | the v5 low-risk guard verbatim (`failure_cost==low`, `semantic_correctness!=critical`, `ambiguity<=3`, `implementation_scope==small`, `behavior_preservation!=required`, `cross_layer==false`, `diagnosis_required==false`, `assumptions_must_be_explicit==false`, `production_feature==false`, safety leaves) | `economy`, effort `low` |
| `bounded-implementation-worker` | down | `worker`, authored `high` | `implementation_scope IN [small, medium]`, `ambiguity<=6`, `failure_cost!=high`, safety leaves | `standard`, effort `medium` |
| `bounded-specifier` | down | `specifier`, authored `high` | `implementation_scope==small`, `ambiguity<=3`, `failure_cost!=high`, safety leaves | `standard`, effort `medium` |
| `critical-escalation` | up | `specifier` and `reviewer`, authored `high` | exactly `failure_cost==high`, `semantic_correctness==critical`, `ambiguity>=7` | `max`, effort unchanged |

The safety leaves are `security_sensitive==false` and
`destructive_operation==false`. Down rules only apply to `analysis`/
`implementation` intents, so review-intent workers are never lowered.

`validate_capacity_assignment` enforces the hard invariants structurally:
rule names are unique and equal the required set with their declared
`direction`; every `match` is a flat `all` list; every down rule contains both
safety leaves verbatim, targets only non-independent-review worker/specifier
steps, and assigns a tier strictly lower than every authored tier it selects;
the up rule's guard equals the three escalation leaves exactly, targets only
specifier/reviewer steps, assigns only `max` and keeps effort `high`; every
assigned tier and effort exists in each targeted family profile. Assignment
only ever keeps or lowers reasoning effort (`min` of authored and assigned).
`resolve_backend_bindings` independently refuses an assignment that lowers an
independent-review step or raises anything other than a specifier/reviewer to
`max`. A project risk floor is applied before rule evaluation, so a floor can
only block a downgrade; security-sensitive and destructive-operation signals
also escalate the *selected route itself* to `CODEX_CLAUDE_CODEX`.

Successful results include `capacity_assignments` with
exactly one ordered entry per plan step: `step_id`, `authored_capacity_tier`,
`assigned_capacity_tier`, the matching `rule` (or `null`), a human-readable
`decision_reason`, the classification-guard evidence (`guard_predicates_passed`
and `blocking_predicates`, each `{rule, field, operator, value, actual}`,
accumulated in order over every candidate rule evaluated up to and including
the winner), and the
concrete `selected_model_profile` (`{family, capacity_tier, model,
context_window_tier}`) resolved for the assigned tier. `validate_route_task_result`
recomputes this entire structure from scratch and rejects any result whose
`capacity_assignments` do not match exactly, so a forged or hand-edited
assignment (including forged evidence) can never pass. The authored
`execution_plan` itself is unchanged. Malformed rules and unsupported profile
mappings fail policy bootstrap.

Plans never name a tool, concrete model, or context-window tier. The policy's
`execution_backends.<family>.model_profile_ref` resolves to a centralized
`model_profiles` entry. Each profile declares its family, supported reasoning
and context values, and `capacity_models.<capacity_tier> -> {model,
context_window_tier}`. Both profiles define all five tiers. Defaults are
cost-first: Claude `light`/`economy` use Haiku, `standard` Sonnet, and
`high`/`max` Opus 5.5; Codex `light`/`economy`/`standard` use Luna, `high`
uses Sol (`gpt-6.1-sol`) and `max` uses Astra (`gpt-6-astra`). A successful result's `backend_bindings` contains only the
families and assigned capacity tiers actually selected for that result, for
example:

```yaml
codex:
  tool: spawn_subagent
  capacity_models:
    standard:
      model: openai-codex/gpt-6-luna
      context_window_tier: standard
```

Claude bindings additionally carry the existing `mutation_modes` mapping.
The prompt and extension consume these bindings generically and never
hard-code model selection.

## Global vs. project-local policy

`routing/default-policy.yaml` is global and reusable across projects. A
project may add local constraints via `.pi/routing.yaml`, validated against
`schemas/project-routing.schema.json`. This file is **constraints-only**:

- It may declare `critical_invariants`, `architecture_constraints`,
  `protected_areas`, and extra `validation.commands` — all additive, never a
  replacement for global requirements.
- It may declare `risk_overrides`, which are **floors**, not replacements:
  - Ordered fields (`failure_cost`, `semantic_correctness`, `ambiguity`,
    `implementation_scope`, `behavior_preservation`) raise a task's effective
    value to at least the given floor, using each field's schema enum order
    (e.g. `low < medium < high`). They can never lower it.
  - Safety booleans (`cross_layer`, `diagnosis_required`,
    `assumptions_must_be_explicit`, `production_feature`,
    `security_sensitive`, `destructive_operation`) may only be forced to
    `true`, OR-merged with the task's own value. Setting one to `false` is
    rejected by the schema.
- It may **never** override `task_type`, `implementation_required`,
  `many_routine_decisions`, routes, precedence, fallback, capacity/model/backend
  policy, execution or worktree isolation, mutation permissions, review
  categories, or completion requirements.
  `schemas/project-routing.schema.json` sets `additionalProperties: false`
  and excludes these keys entirely, so any attempt fails closed.

Route selection under project constraints:

1. Route the task's original classification under the global policy; call
   the result `base_route` (the matched route, or `fallback_route` if
   nothing matched).
2. Apply `risk_overrides` floors to produce a constrained classification, and
   route that under the global policy too.
3. Build the candidate set: the union of routes matched by both
   classifications, plus `base_route` itself. `base_route` must be included
   explicitly even when it is `fallback_route` and matched nothing, otherwise
   an override that newly matches only a lower-precedence route than the
   base's own fallback would silently downgrade routing.
4. Select the earliest route in the global `precedence` list from that
   candidate set.

Because `base_route` is always a member of the candidate set, the selected
route's precedence rank can never be worse (later) than `base_route`'s —
this can only escalate or preserve a route relative to the unconstrained
classification, never downgrade it.

## Adaptive lead-agent delegation

Top-level routing and nested delegation are intentionally separate. The four
routes still deterministically decide which family leads, which authored steps
run, their capacity/reasoning/context settings, permissions, and whether an
independent reviewer is mandatory. `routing/delegation-policy.yaml` then
governs whether a lead execution step should execute directly or use bounded
subagents.

The policy's objective is to minimize wall-clock time, token cost, duplicated
work, and integration risk without weakening correctness. For each meaningful
unit the lead chooses one of `DIRECT`, `DELEGATE`, `PARALLEL`, `BLOCKED`,
or `SEQUENTIAL` and uses the qualitative utility:

```text
delegation_utility =
    parallelism + context_isolation + specialization
    - coordination_cost - shared_state_risk - verification_cost
```

The default is the smallest useful fan-out. Parallel fan-out is capped at four
workers; larger team-style execution is reserved for genuinely repository-wide
independent work. Deterministic tests, compilers, schemas, linters, and queries
are preferred over spawning another agent when they can answer the question.

A Codex lead launched through `spawn_subagent` cannot spawn nested subagents,
so it executes DIRECT or SEQUENTIAL; Codex parallelism comes from the parent's
waves of independent top-level tasks. A Claude lead running a mutating step
through `ClaudeWorker` may parallelize disjoint subtasks with Claude Code's native Agent tool when
independence, concurrency, context isolation, or specialization justify the
coordination cost. Claude nested worker
roles use abstract `high` / `standard` / `economy` / `light` capacity tiers resolved
only through the existing centralized Claude model profile; model ids are never
duplicated in the delegation policy.

Every nested worker receives explicit scope, write ownership, relevant
repository constraints, guardrails, completion criteria, deterministic
verification, and a compact return contract. Parallel mutating workers require
disjoint scopes and isolated worktrees. Overlapping writes are serialized or
integrated by the lead.

Top-level independent reviewers remain mandatory whenever the selected route
authors them. Additional nested review is selective: security, migrations,
scientific/statistical logic, complex concurrency, architectural changes, and
subtle regression risk are high-value cases; trivial work does not receive a
reviewer merely for redundancy.

Focused local failures go back to the same context-rich worker; architectural
mistakes return to the lead for re-planning; repeated failures escalate
capability or use fresh independent reasoning. Ambiguous requirements are
escalated rather than guessed independently by several workers. Coordination is
event-driven when possible, with polling only when no event mechanism exists.

After a verified batch is integrated, the orchestrator continues with the next
dependency-ready wave inside the authorized scope rather than stopping after a
single subtask or PR.

### Delegation metrics and learning

New run provenance can record `orchestration_metrics`: delegation topology,
lead/worker/cached/total tokens, cost, wall time, worker and coordination time,
conflicting edits, corrective turns, failed attempts, test/CI outcomes,
reviewer findings, human corrections, and final success. Where explicit
baselines are available, `tools/provenance.py` derives coordination token/time
tax, quality gain, and parallel speedup. Parent runs fill
`delegation_topology` (from the peak number of concurrent worker calls),
`wall_clock_ms`, `worker_execution_ms` (sum of worker durations), and
`estimated_serial_time_ms` = wall clock − time with a worker in flight + sum of
worker durations, so `parallel_speedup` is 1 for sequential workers and above 1
when a wave overlapped them. `provenance.add_step`, `update_step`, and
`update_orchestration_metrics` take the per-run file lock shared with parent
events, so concurrent writers never lose updates.

These metrics are observational during a run. They never alter that run's route
or permissions. `benchmarks/delegation/README.md` defines the offline evidence
loop comparing direct execution, 1 worker, 2 workers, 3–4 workers, mixed-model
workers, and same-model workers. Only a reviewed/versioned repository change
may update the delegation policy from accumulated evidence.

## Delegated mutation worktree protocol

Worktrees are an orchestration safety protocol, not a routing input and not a
project override. Before starting a delegated step with
`mutation_permission: allowed`, the orchestrator records a pinned starting
commit and creates a dedicated Git worktree plus a dedicated branch from that
exact commit. The worker receives the prepared path, branch, pinned commit,
allowed path scope, task contract, and validation commands. A worker never
creates or removes worktrees, switches the parent checkout, pushes, merges, or
mutates GitHub state. It commits only when the orchestrator's contract asks it
to; the orchestrator remains responsible for inspecting and integrating the
result.

Every mutation contract has an explicit, minimally sufficient path scope.
Independent mutations may run concurrently only in separate prepared
worktrees with disjoint scopes. Overlapping scopes, dependent changes, and all
integration into the target branch are serialized. Integration is performed
by the orchestrator one result at a time, with conflict resolution, diff
inspection, and relevant validation after each integration. A worker must
stop and report rather than modifying outside its scope.

### Waves

The parent runs independent tasks in waves. Each task declares a write scope
(repository-relative paths or globs). `orchestration_record` actions:

| Action | Effect | Blockers |
| --- | --- | --- |
| `bind_workspace {task_id?, step_id, path}` | Binds a mutating step of a task to its linked worktree (`task_id` may be omitted when one task is routed). A worktree inside a `.git` directory is refused: Claude Code denies every edit there, even in `acceptEdits` mode. | `WORKER_WORKSPACE_INVALID`, `WORKER_WORKSPACE_PROTECTED_PATH`, `WORKER_TASK_UNATTRIBUTED` |
| `declare_wave {wave_id, tasks:[{task_id, write_scope}]}` | Declares routed tasks that may run concurrently; recorded as a `wave` event. | `WAVE_SCOPE_OVERLAP`, `WAVE_SCOPE_INVALID`, `WAVE_LIMIT_EXCEEDED`, `WAVE_TASK_UNROUTED`, `WAVE_WORKERS_IN_FLIGHT`, `AUTONOMY_POLICY_INVALID` |
| `integrate_begin {wave_id}` | Starts integration; worker launches are refused until it ends. | `WAVE_UNKNOWN`, `INTEGRATION_WORKERS_IN_FLIGHT` |
| `integrate_end {wave_id}` | Re-bases the parent-checkout snapshot after the parent's merges and closes the wave. | `INTEGRATION_NOT_STARTED`, `WORKER_SCOPE_UNVERIFIABLE` |

A routed task started while no worker runs and no wave is declared gets its own implicit single-task wave
(`auto-<hex>-<task>`); it is never grown and never lets tasks overlap: only an explicit `declare_wave` does, after its
write-scope, parallel-limit and routing checks. `integrate_begin` and `integrate_end` accept `"auto"` for the one open
implicit wave; with several open it fails with `WAVE_AUTO_AMBIGUOUS` (listing the ids) and with none with
`WAVE_UNKNOWN`. At a non-COMPLETED finish every `declared` or `integrating` wave becomes `abandoned` (`reconciled`,
kind `wave`). COMPLETED is refused with `PARENT_WAVE_INTEGRATING` while a current wave is `integrating`, and with
`PARENT_WAVE_NOT_INTEGRATED` for any task whose bound worktree no integrated wave has covered since
(`phase.workspace_bound`, set by the bind checkpoint and cleared when a wave containing the task is `integrated`; checked
per task, so a missing wave record cannot hide it). `phase.bound_epochs` lists the route epochs that bound a worktree, so
an escalation does not forget the old route's; a read-only task does not block COMPLETED.
The extension never infers the integrate bracket: `integrated` would assert a merge it did not observe.

Worker `activity` entries replace the lead's directive and feedback events. At start they carry `route`,
`prompt_digest`, `goal` (the contract's `Goal:` line) and `prompt_excerpt` (first 300 characters after the task tag and
goal line); on return `result_summary` (first 600 characters of the final text), `result_digest` and `commit_sha` when
the result reports one. Text is single-line with control characters removed. Secret-like spans (provenance's content
patterns plus GitHub fine-grained and GitLab tokens, Google API keys, JWTs, `password=`/`token=`/`secret=`
assignments with a value of 16 or more characters without spaces, URLs with credentials, npm tokens and Stripe live keys) are masked in the whole text, bounded to eight
times the recorded size, before the cut; an unbroken run of 16 or more characters left at the cut is dropped. The full
text lives only in the Pi session transcript. A failed step may become `completed` only when, after its last failure, it
was started again on the same route and that start returned without error: the failure is kept in `worker_overrides`
with `reason: "retry_succeeded"` and `by: "extension"` or `"lead"`.

Scope overlap is conservative: an entry's scope is its literal directory
prefix up to the first glob segment, and two entries overlap when one prefix
contains the other (`src/a/**` and `src/b/**` are disjoint; `src/*.ts`
overlaps everything under `src/`). The wave limit is
`limits.parallel_tasks` from the target's `.pi/autonomy.yaml`
(`tools/autonomy_policy.py limits --worktree <target>`, default 3).

Worker calls are attributed to a task and step: `spawn_subagent`
`label: "<task_id>:<step_id>"`, a `ClaudeWorker` (or read-only `AskClaude`)
prompt starting `[pico-task <task_id>:<step_id>]`, and `message_subagent`
through the spawn it continues. The lifecycle sets or verifies `model` and `thinking` for both
backends from the step's `capacity_assignments` entry and
`backend_bindings[family].capacity_models[assigned_capacity_tier]`, whatever
tiers the policy defines. Workers of different tasks may overlap only inside
the declared wave (`WAVE_UNDECLARED`). Mutating Codex workers must name their
bound worktree in `task` (`WORKER_WORKSPACE_UNSTATED`).

The policy binds the Claude family to `ClaudeWorker`
(`execution_backends.claude.tool`), a tool registered by the lifecycle
extension (`extensions/claude-worker.ts`). Each call spawns the
preflight-pinned executable (`backend_executable` of the preflighted identity,
re-verified with `verify_backend` immediately before the call) as
`claude -p --model <claude-code id> --effort <assigned effort>
--permission-mode <acceptEdits|default> --tools ... --allowedTools ...
--disallowedTools ... --append-system-prompt <worker contract>
--output-format json --no-session-persistence`, with the prompt on stdin
and `cwd` set to the bound worktree (`WORKER_WORKSPACE_UNBOUND` for an
unbound mutating step; a read-only step without one uses the target
checkout). The model cannot pass a directory, mode, or executable.
Mutating steps allow Read, Edit, Write, Glob, Grep, Bash, and Agent;
read-only steps allow Read, Glob, and Grep. `Bash(git push:*)`,
`Bash(gh:*)`, `Bash(git worktree:*)`, `git branch -d/-D/--delete`, git
invocations that redirect or reconfigure git (`git -C`, `git -c`,
`--git-dir`, `--work-tree`, `--exec-path`), `git remote add/set-url`, and
`env`/`command`/`exec` wrappers around `git` and `gh` are denied. The deny
list is best-effort defense in depth, not a boundary: Claude Code matches Bash
rules by command prefix, so a worker with Bash can still reach the same effect
through a script, another interpreter, or the network. Other parent-only
operations (merges, writes outside the worktree through shell commands) rely
on the worker contract and the parent-checkout snapshot comparison, which sees
only the parent checkout, not `$HOME`, other repositories, or the network.

The worker process does not inherit Pi's environment. `buildWorkerEnv`
forwards only `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`,
`LANGUAGE`, `LC_*`, `TERM`, `COLORTERM`, `TZ`, `TMPDIR`/`TMP`/`TEMP`,
`XDG_{CONFIG,DATA,CACHE,STATE}_HOME`, `NODE_EXTRA_CA_CERTS`,
`SSL_CERT_FILE`, `SSL_CERT_DIR`, the proxy variables
(`HTTP(S)_PROXY`, `NO_PROXY`, `ALL_PROXY`, upper and lower case), and
`ANTHROPIC_*` / `CLAUDE_*` (Claude Code configuration and API-key or OAuth
authentication; a stored `claude` login is found through `HOME`). Everything
else is withheld, including `GH_TOKEN`, `GITHUB_TOKEN`,
`GH_ENTERPRISE_TOKEN`, `GIT_ASKPASS`, `SSH_AUTH_SOCK`, `NPM_TOKEN`, Git
overrides such as `GIT_DIR`, cloud credentials (`AWS_*`, `GOOGLE_*`) and
PICO's own `PI_*` settings, which the worker does not read. PICO verifies
Claude authentication only through `claude auth status`, so third-party
provider modes (Bedrock, Vertex) are not forwarded by default; set
`PI_ORCHESTRATOR_WORKER_ENV_ALLOW` to a comma-separated list of exact
variable names (for example `JAVA_HOME,AWS_PROFILE`) to forward more. A name
in that list is forwarded even if it is a credential: that is the user's
explicit choice. An entry that is not a variable name blocks the call with
`WORKER_CONFIG_INVALID` before Claude Code starts. The tool result records
`env_dropped_count` and `env_dropped` (the withheld names, never values; at
most 100).

`PI_ORCHESTRATOR_WORKER_SANDBOX=1` adds `--settings
{"sandbox":{"enabled":true,"failIfUnavailable":true}}`, so Claude Code runs the
worker's Bash commands in its OS sandbox and exits at startup when the sandbox
cannot start instead of running them unconfined (any value other than unset,
`0` or `1` is `WORKER_CONFIG_INVALID`). It is off by default because the
sandbox restricts network access and needs platform support (bubblewrap on
Linux), so dependency installs or checks that need the network can fail
inside it. The settings keys were read from the settings schema of Claude
Code 2.1.304; the setting has not been exercised end to end against a real
worker run or older releases. We recommend running workers in
an OS sandbox limited to the worktree, with this setting, a container, or
bwrap. Claude Code protects every `.git`
directory and denies edits there even in `acceptEdits` mode, so a mutating
step bound to a worktree inside `.git` is refused before Claude starts
(`WORKER_WORKSPACE_PROTECTED_PATH`); the permission mode is never widened to
compensate. Calls Claude Code still denies are listed in the tool result
(`denied_calls`, for example `Write /path/CHANGELOG.md`).
`PICO_REAL_CLAUDE_SMOKE=1 node scripts/claude-worker-smoke.ts [claude] [model]`
checks this against the real CLI with one cheap call. The Pi process directory never changes,
so ClaudeWorker calls (declared `executionMode: "parallel"`) run concurrently
with each other and with `spawn_subagent`. Abort kills the child (SIGTERM,
then SIGKILL after 5 s); `PI_CLAUDE_WORKER_TIMEOUT_MS` (default 30 min) and
an 8 MiB stdout cap bound each run (`WORKER_TIMEOUT`, `WORKER_ABORTED`,
`WORKER_OUTPUT_LIMIT`). On launch the lifecycle records an observational
`in_progress` worker event (canonical model, `backend: claude-code`,
`backend_model`, `reasoning_effort`); on return it records `usage` with
duration, input/output/cache-read tokens, and Claude Code's `total_cost_usd`
as `estimated_cost_usd`, and the call counts toward
`peak_concurrent_workers`. The parent still records each worker's terminal
status. `AskClaude` stays accepted for read-only routed steps; a mutating step
called through it is blocked with `CLAUDE_WORKER_REQUIRED`.

Publishing follows `.pi/autonomy.yaml`: with `pr_granularity: batch` (default)
an integrated wave is merged into a local `pico/batch-<n>` branch and gets one
push and one PR (`Refs #N` for each issue, or `Closes #N` under
`issue_reference: closes`) when `push_branch`/`create_pr` allow it; `task`
opens one PR per task. After a push the orchestrator records `waiting_ci` and
continues with the next wave, stacked on the batch branch or started from the
base when independent. A CI failure becomes a `ci_fix` task in a later wave;
only merge waits for CI. At `limits.open_prs` (default 2) open batch PRs, new
waves stack on the newest batch branch instead of opening another PR.

`issue_reference` (`refs` by default, or `closes`) decides whether a published
PR closes its issue on merge. The publication gate in
`tools/github_integration.py --publish` requires a line starting `Refs #N`,
or `Closes #N` when closing is authorized, and finds every issue the body
would close: a keyword (`close[sd]`, `fix(es|ed)`, `resolve[sd]`, any case)
followed by an optional colon, optional Unicode whitespace and `#N`,
`owner/repo#N` or `https://github.com/owner/repo/issues/N`. Each closed issue
must be authorized, or the gate refuses with `PUBLICATION_BODY_INVALID`:

- an in-process caller can name one issue (`closes_issue=N`, which
  `workspace_contracts.publish` passes for a task with `closes_issue: true`;
  there is no command-line flag), and then only that issue may close;
- otherwise, when the policy says `closes`, each issue must have its own
  `Closes #N` line, so a batch PR can close every issue it resolves.

Closed issues must be in the PR's own repository. `issue_reference` is read
from `.pi/autonomy.yaml` at the **default branch's** current commit **as the
GitHub API reports it** (never the caller's `--base`; see below), through the
authenticated `gh` adapter (`github_sync.default_branch`: `gh api -X GET
repos/<repo>`; `github_sync.branch_head`: `gh api -X GET
repos/<repo>/git/ref/heads/<default>`;
`github_sync.policy_file_at`: `gh api -X GET
repos/<repo>/contents/.pi/autonomy.yaml?ref=<sha>`, both fixed argv with
shape validation, pinned to `--hostname github.com` and run without
`GH_HOST`), and validated against the schema. Local git is never asked
about the base branch: every worktree shares the repository's refs and git
config, so a forged `refs/heads/<base>` or `refs/remotes/...`, a `git
replace`, or a `core.sshCommand`, `remote.<name>.uploadpack` or
`include.path` that serves a fake remote cannot authorize closing, and
neither can an uncommitted edit or a policy committed on the `pico/` branch.
A default branch or policy that GitHub does not have means `refs`. GitHub
reports a symlink at `.pi/autonomy.yaml` as `type: symlink` (or, for a
symlink to a file in the repository, as that file under another `path`);
both are refused with `AUTONOMY_POLICY_PATH_UNSAFE`, as is a symlinked `.pi`
or policy in the checkout (a dirty copy is ignored, see below). Base branch names are limited to
`[A-Za-z0-9._-]` segments separated by `/` (`PUBLICATION_REF_INVALID`). The
policy is consulted only when the body would close an issue the caller did
not name. The dry-run plan records `base_commit`, `policy_branch`,
`policy_commit`, `policy_blob`, `issue_reference`, `closes_issues` and the body
hash, so the reviewed `plan_hash` binds all of them. Any merge to the base or
the default branch between the dry run and the apply makes the apply fail
with `GITHUB_STATE_CONFLICT`, even for a PR that only says `Refs #N`; review a
fresh dry run and apply again. A workspace task without `closes_issue` reads
the same default-branch policy to choose its body, and `workspace_contracts
publish` returns that choice as `issue_reference_decision` (`closes`,
`source: task | repository`, `policy_branch`, `policy_commit`,
`policy_blob`).

**Publication permissions (`push_branch`, `create_pr`) come from the same
default-branch policy** (issue #39). `github_integration.publish` asks the API
for the repository's default branch (`repos/<repo>` `default_branch`; the
response must name the same repository), reads that branch's head commit and
its `.pi/autonomy.yaml` once, and that one blob decides both `issue_reference`
and whether the PR may be pushed and opened. Neither the working tree nor the
caller's `--base` is authority. `--base` only chooses where the PR points: a
worker's `pico/` branch reaches GitHub through a single grant and could carry
any policy, so a policy read at the caller-named base would let that branch
authorize itself (and close issues). A worker can also edit its tracked copy,
commit a grant on its `pico/` branch (which reaches the integrated branch when
the lead integrates), or symlink `.pi`; none of that matters. Order of checks,
before any local ref is inspected:

1. A symlinked `.pi` or `.pi/autonomy.yaml` in the checkout is refused with
   `AUTONOMY_POLICY_PATH_UNSAFE` (cheap defence in depth). A modified, deleted
   or untracked copy is not refused: it has no authority, and refusing it
   would break a Garden toggle or `init_project`'s uncommitted policy.
2. The default-branch policy is read; a symlink, a `type`/`path` mismatch, a
   directory or an unreadable file is `AUTONOMY_POLICY_PATH_UNSAFE` or
   `AUTONOMY_POLICY_INVALID`, and a schema-invalid policy is
   `AUTONOMY_POLICY_INVALID`.
3. A missing default branch or policy, or a policy that does not set both
   permissions to `true`, is `PUBLICATION_NOT_AUTHORIZED`. Nothing overrides
   it: the Garden one-shot grant was removed (#59, see "Trust boundary
   summary" below).

The plan records `policy_branch`, `policy_commit`, `policy_blob` (always,
replacing the earlier `issue_policy_blob`), `publication_permissions` (the two
booleans the policy evaluated to), `base_commit` and `authorization` (always
`policy`); all are in the hashed plan. The publication record's
`authorization` field carries `source: policy`, the `permissions` checked and
the `policy_commit` that granted them; records written before #59 may show
`source: one_shot_grant` with `granted_by` and `granted`. A merge that changes the default branch between the dry run
and the apply changes `policy_commit` and so yields `GITHUB_STATE_CONFLICT`; a
change that withdraws the permissions is `PUBLICATION_NOT_AUTHORIZED` at apply
time.

*Root of trust.* The repository's default branch on GitHub is now the root of
trust for publication. Whoever can push to it, merge into it, or change which
branch is the default (a repository admin) controls `push_branch`,
`create_pr` and `issue_reference` for every publication. Protect the default
branch: require pull request review and disallow direct pushes (and restrict
who can change the default branch or edit branch protection). PICO cannot
verify these settings; they are the operator's responsibility.

*Drift.* A checkout whose `.pi/autonomy.yaml` (committed or dirty) differs from
the authorizing policy is reported but not refused: `publish` returns
`policy_drift`, the sorted permission names that differ. It is informational,
not part of the plan hash, and can never grant more than the authorizing
policy. Refusing on drift (a candidate `AUTONOMY_POLICY_DRIFT`) was rejected:
it would block publishing the very PR that proposes a policy change.

Who else reads autonomy permissions (issue #39 audit):

| Reader | Effect | Source |
| --- | --- | --- |
| `github_integration.publish` (push, draft PR); reached by `--publish`, `workspace_contracts.publish` and the Garden `publish` action | GitHub write | default-branch policy on GitHub (changed) |
| `backlog github-pr-create` | opened a PR for any base and head with no permission check, dry run or plan hash | **removed**; PRs are opened only by `publish`, and `github-pr-link` links the result. Removal was smaller and safer than rebuilding a second gated path |
| Garden one-shot grant | in-process override for one reviewed plan | **removed** (#59): whoever held the session token, an agent included, could confirm it |
| `github_integration.execute`, `backlog github-push` | GitHub write | default-branch policy on GitHub (#58), see "Issue and comment writes" below |
| `headless_run.mutation_permitted` | decides whether `start` needs `--allow-mutation` for a local run | working tree, left: it can only add a prompt for the operator, it authorizes no GitHub write |
| `autonomy_policy.limits` (`parallel_tasks`, `open_prs`, `pr_granularity`) | local scheduling | working tree, left |
| Garden permissions panel | display and `set_permission`/`commit_policy` | checkout, left; its preview and panel say that publication and issue and comment writes ignore it until the change is committed and pushed to the default branch |

**Issue and comment writes (#58).** `github_integration.execute` and
`backlog github-push` read the same default-branch policy on GitHub as
publication, through the same API path, before any write:

| Operation | Needs |
| --- | --- |
| `create_issue`, `update_issue`, `close_issue`, `reopen_issue`, `create_label`, `add_label`, `remove_label`; `github-push` creating, editing, closing or reopening an issue | `create_github_issues` |
| `comment_issue` | `comment_on_issue` |
| `comment_pr` | `comment_on_pr` |
| `update_pr` (title or body) | `create_pr` |

`execute` checks every write in the plan, the dry run included, so a reviewer
never approves a batch that cannot run; a no-op batch needs nothing. A missing
default branch or policy, or a permission that is not `true`, is
`GITHUB_WRITE_NOT_AUTHORIZED`, and the reason names each missing permission;
an unreadable or unsafe policy is `AUTONOMY_POLICY_INVALID` or
`AUTONOMY_POLICY_PATH_UNSAFE`. Closing an issue still also needs
`--allow-destructive`. `github-push` checks only when it is about to write, so
an in-sync push needs nothing. The run record's `authorization` carries the
permissions checked and the `policy_commit`. There is no override. With the
`manual` profile every permission is off, so backlog sync, issue comments and
closing an issue on backlog completion are refused until the repository's
default branch sets the keys. `report_pico_issue.py`, which files a report you
wrote in the PICO repository itself, is not governed by the target's policy.

**Trust boundary summary.** GitHub write authority rests on the default branch
on GitHub (see "Root of trust" above) and nowhere else. The Garden one-shot
publication grant was removed (#59): it trusted whoever held the session
token, and `pico watch` prints the token, so a process running as the user,
including a lead running bash outside a sandbox, could start its own Garden
and confirm a grant that the record then showed as `granted_by: garden`.
Agent-side tool denylists do not fully stop a same-user process: one can still
call `gh` directly, so they are not a substitute for branch protection.
Running workers in a sandbox limited to their worktree is tracked in #63.
The containment tests this needs, and the separation of planning, execution, publication
and promotion authority for the queue dispatcher (#34), are in
[`docs/containment-and-authority.md`](containment-and-authority.md).

The trust boundary stops at the push. The branch push itself still runs
through local git (`git push` with the checkout's remote and config), so a
worker that can write the shared `.git/config` can still redirect or alter
the push; the gate cannot protect against that. Isolating workers from the
parent repository's git config is tracked with worker isolation (S1) and #39.
The gate also trusts the lead's own environment and `gh` setup (`GH_CONFIG_DIR`,
the `gh` found on `PATH`, and its tokens) in the same way it trusts the user's
home directory.
The match ignores Markdown structure: GitHub
does not act on keywords in code spans or HTML comments, but the gate refuses
them there too rather than reimplement GitHub's parser. Backlog-generated PR
bodies stay `Refs #N` in every mode, because a backlog item's issue closes
through `github-push`.

Specifier and reviewer steps remain read-only. Independent review receives the
integrated diff/repository state and validation evidence in a fresh context;
it does not edit either the implementation worktree or target branch. A repair
is a newly contracted mutation step (and dedicated worktree when delegated),
not an untracked reviewer edit.

Cleanup happens only after work is integrated or deliberately abandoned and
needed evidence is retained. The orchestrator removes worktrees before local
branches, never uses force cleanup on a dirty/unmerged worktree without human
approval, and accounts for untracked files, submodules, nested repositories,
platform file locks, and process current-working-directories that can make
cleanup unsafe or fail. Cleanup failure is reported; it does not justify data
loss. None of this protocol grants workers permission to push, merge, open or
close issues/PRs, or otherwise mutate GitHub.

## Task-classifier benchmark

`benchmarks/classification/` measures the probabilistic boundary that precedes
deterministic routing: converting a raw engineering request plus repository
evidence into the bounded classification fields required by
`schemas/task.schema.json`.

The benchmark is model-independent and does not affect production routing.
Candidate classifiers emit JSONL predictions; `evaluate.py` validates them
against the real task schema and scores field accuracy, route agreement,
policy-relative under/over-routing, and the complete downstream execution
decision (route + primary intent + assigned capacity tiers) through the shared
`tools/routing_engine.py`. Gold and predicted classifications must satisfy the
same escalation-evidence rule as `route_task`; a prediction that does not is
reported as invalid. The report includes per-field escalation rates
(predicted vs gold), and cases may pin an `expected_decision`.

```sh
python3 benchmarks/classification/evaluate.py \
  --predictions path/to/predictions.jsonl \
  --require-all
```

See `benchmarks/classification/README.md` for the prediction contract and
benchmark methodology.

## Fixtures

`tests/routing-cases.yaml` contains three kinds of fixtures, each consumed
by `tools/validate_routing.py`:

- `cases` — complete, schema-valid tasks with `expected_matches` (every
  route whose `match` expression is true) and `expected_route` (the route
  selected after precedence/fallback).
- `override_cases` — a base task plus a `project_routing.risk_overrides`
  fragment, with expectations for both the unconstrained and the
  overrides-applied outcome.
- `invalid_project_routing_examples` — project-routing fragments that must
  be rejected by `schemas/project-routing.schema.json`, proving unknown or
  weakening keys fail closed.

## Dependencies

Both static validation and runtime routing require `PyYAML` and
`jsonschema`, pinned in `requirements-dev.txt`.

## How to validate (static)

```sh
python3 -m pip install -r requirements-dev.txt
python3 tools/validate_routing.py
```

`tools/validate_routing.py` is a small, deterministic, read-only script. It
resolves the repository root from its own path (so it runs correctly from
any working directory), loads YAML with a safe loader that rejects duplicate
keys and arbitrary constructors, and performs no network access, no eval,
and no model invocation. It checks:

1. `schemas/task.schema.json`, `schemas/project-routing.schema.json`,
   `schemas/execution-plan.schema.json`, and
   `schemas/delegation-policy.schema.json` are valid JSON Schemas; the
   delegation policy is cross-consistent with centralized model capacity tiers.
2. `routing/default-policy.yaml`'s match-expression AST is well-formed:
   every field exists in the task schema, every operator/value is
   compatible, and routes/precedence/execution/fallback are exactly the
   four known routes.
3. Every `execution.<ROUTE>.primary_intents.<analysis|implementation|review>`
   plan is schema/semantically valid, matches canonical topology, references
   family-supported capacity/reasoning/context metadata, and keeps that
   metadata identical across primary intents. Capacity rules are structurally
   safe and retain the frozen route/plan invariants. Every backend profile
   reference resolves to the same family; standard/high remain mandatory and
   every configured capacity resolves to a model and context-window tier.
4. `templates/project-routing.yaml` validates against
   `schemas/project-routing.schema.json`.
5. `prompts/orchestrate.md` mirrors the schema's required field list, the
   four route headings, the fallback route, and the policy's review finding
   categories, and contains no absolute filesystem paths.
6. Every fixture in `tests/routing-cases.yaml` evaluates to its expected
   result using the deterministic evaluator (no LLM involved).

Exit status is 0 on success; otherwise every failure is printed and the
script exits 1.

## Projects may provide local constraints through

- `AGENTS.md` — project architecture, invariants, terminology, validation
  commands, and engineering rules (read and obeyed by the orchestrating
  agent, not machine-validated by this repository).
- `.pi/routing.yaml` — structured routing constraints, validated against
  `schemas/project-routing.schema.json` as described above. See
  `templates/project-routing.yaml` for a starting point.

## Runtime routing (Pi extension)

Route selection is never performed by an LLM. At orchestration time, the
`extensions/model-router.ts` Pi extension registers a `route_task` tool that
delegates every routing decision to the same deterministic evaluator the
static validator uses:

```
extensions/model-router.ts  (thin Pi tool adapter, TypeScript)
        │  spawns, no shell, JSON over stdio
        ▼
tools/route_task.py         (thin stdio adapter, Python)
        │  imports
        ▼
tools/routing_engine.py     (the evaluator: matching, precedence, fallback,
                              risk-floor overrides, no-downgrade selection,
                              execution-plan validation/materialization,
                              post-route capacity assignment,
                              backend-binding resolution)
```

`extensions/model-router.ts` contains no route predicates, precedence logic,
risk-floor logic, or hand-copied task-schema fields: its tool parameter
schema is loaded directly from `schemas/task.schema.json` (via TypeBox's
`Type.Unsafe`), and its only job is to spawn `tools/route_task.py`, forward
`{"classified_task": ...}` on stdin, parse the JSON result, and return the
full parsed result in structured `details`. Before returning any `ok: true`
result, it re-checks the result's `execution_plan` against
`schemas/execution-plan.schema.json` (also via `Type.Unsafe`, so no second
JSON Schema library is introduced) -- a missing or schema-invalid plan
crossing the subprocess boundary is turned into a tool-level `ok: false`
failure with no usable `details.result`, never passed through. This is a
structural boundary check only; the semantic rules (role/intent/mutation/
dependency/independent-review invariants) live solely in
`tools/routing_engine.py` and are not reimplemented in TypeScript. The
LLM-visible tool text is the same JSON for normal-sized results; for
unusually large results it is a bounded compact JSON summary that preserves
routing-critical fields such as `ok`, `policy_version`, `selected_route`,
`execution_plan`, `capacity_assignments`, `backend_bindings`,
`fallback_applied`, and `matches`, while omitting bulky fields already
available in `details`. It resolves `tools/route_task.py`,
`schemas/task.schema.json`, and `schemas/execution-plan.schema.json`
relative to its own file location, so this repository's full tree (not just
the `.ts` file in isolation) must be present wherever the extension is
loaded from, and it reads the project-local config from `join(ctx.cwd,
CONFIG_DIR_NAME, "routing.yaml")` (`CONFIG_DIR_NAME` from
`@earendil-works/pi-coding-agent`, currently `.pi`).

### `route_task` input/output

Input (stdin JSON, or the tool's `classified_task` parameter):

```json
{ "classified_task": { "id": "...", "goal": "...", "task_type": "...", "...": "..." } }
```

Runtime adapter stdout JSON, exactly one object; the Pi tool returns this full object in `details.result`:

```json
{
  "ok": true,
  "policy_version": 4,
  "classification": { "base": { "...": "..." }, "effective": { "...": "..." } },
  "matches": { "base": ["CODEX"], "effective": ["CODEX"] },
  "fallback_applied": false,
  "selected_route": "CODEX",
  "execution_plan": {
    "schema_version": 3,
    "independent_review_required": false,
    "steps": [
      {
        "id": "codex_worker",
        "order": 1,
        "role": "worker",
        "family": "codex",
        "intent": "implementation",
        "capacity_tier": "standard",
        "reasoning_effort": "high",
        "context_strategy": "full",
        "fresh_context": true,
        "mutation_permission": "allowed",
        "depends_on": [],
        "independent_review": false
      }
    ]
  },
  "capacity_assignments": [
    {
      "step_id": "codex_worker",
      "authored_capacity_tier": "standard",
      "assigned_capacity_tier": "standard",
      "rule": null,
      "decision_reason": "authored tier kept: ops-light blocked by 2 unmet guard predicate(s); low-risk-worker blocked by 1 unmet guard predicate(s)",
      "guard_predicates_passed": [
        { "rule": "low-risk-worker", "field": "cross_layer", "operator": "==", "value": false, "actual": false }
      ],
      "blocking_predicates": [
        { "rule": "ops-light", "field": "task_type", "operator": "IN", "value": ["vcs_operation", "issue_management", "config_edit", "documentation", "ci_fix"], "actual": "debugging" },
        { "rule": "ops-light", "field": "diagnosis_required", "operator": "==", "value": false, "actual": true },
        { "rule": "low-risk-worker", "field": "diagnosis_required", "operator": "==", "value": false, "actual": true }
      ],
      "selected_model_profile": {
        "family": "codex",
        "capacity_tier": "standard",
        "model": "openai-codex/gpt-6-luna",
        "context_window_tier": "standard"
      }
    }
  ],
  "backend_bindings": {
    "codex": {
      "tool": "spawn_subagent",
      "capacity_models": {
        "standard": {
          "model": "openai-codex/gpt-6-luna",
          "context_window_tier": "standard"
        }
      }
    }
  },
  "project_constraints": { "risk_overrides": {}, "...": "..." }
}
```

or, for an invalid task or project config:

```json
{ "ok": false, "errors": [{ "path": "classified_task.failure_cost", "message": "..." }] }
```

A schema-valid classification with an unjustified escalating value adds
`"blocker_code": "CLASSIFICATION_EVIDENCE_MISSING"` and
`"missing_evidence_fields"` to that shape (see "Classification rubric and
escalation evidence").

`classification.base` is the task as classified; `classification.effective`
is the same task after any `.pi/routing.yaml` `risk_overrides` floors are
applied. In the Pi tool, these complete fields are always available in
`details.result`; they may be omitted from the LLM-visible text content when
needed to keep that content bounded. `matches` lists which routes independently matched each
classification. `fallback_applied` is true when `selected_route` was not
itself matched by either classification (i.e. it came from `fallback_route`
or from the base classification's own route being preserved against a
downgrade).

`execution_plan` is `routing/default-policy.yaml`'s
`execution.<selected_route>.primary_intents.<primary_intent>` plan, deep-copied
and revalidated against `schemas/execution-plan.schema.json` before being
returned (see "Execution plans" above). `primary_intent` is derived centrally
from `classification.effective` and is never selected by the caller.
`policy_version` identifies the validated policy contract used.
`capacity_assignments` records the deterministic post-route tier selected for
every authored step, along with the decision evidence and resolved model
profile described above. The `backend_bindings` map resolves each used family
and only its assigned capacity tiers through `execution_backends` +
`model_profiles`; alongside each assignment's own `selected_model_profile`,
these are the only places a concrete model or context-window tier appears in
the result. `project_constraints` is the validated
`.pi/routing.yaml` contents (or `{}` if none), meant to be passed into worker
contracts. Telemetry is intentionally absent from `route_task` output; observed
routing/step counters belong only in run provenance.

An `ok: false` result means: repair the classification or project config
and call `route_task` again -- never select a route without it. See
`prompts/orchestrate.md` for the full protocol an orchestrating LLM follows:
it classifies tasks, but `route_task` alone decides which route each task
takes.

### Quickstart from another repository

From the project you are already working on, stream the helper from GitHub. It
fetches or updates the orchestrator cache, then starts Pi in the target project
with the router extension loaded:

```sh
cd existing-project
ORCH_RAW=https:$(printf /)$(printf /)raw.githubusercontent.com$(printf /)dhuzard$(printf /)pi-coding-orchestrator$(printf /)main$(printf /)scripts$(printf /)use-latest.sh
curl -fsSL "$ORCH_RAW" | bash -s -- run .
```

Recommended first-time setup in that project:

```sh
cd existing-project
ORCH_RAW=https:$(printf /)$(printf /)raw.githubusercontent.com$(printf /)dhuzard$(printf /)pi-coding-orchestrator$(printf /)main$(printf /)scripts$(printf /)use-latest.sh
curl -fsSL "$ORCH_RAW" | bash -s -- init .
curl -fsSL "$ORCH_RAW" | bash -s -- run .
```

`init .` is a one-time bootstrap for the target repo. It writes starter
`AGENTS.md`, `.pi/routing.yaml`, and `.pi/orchestrator-pr-workflow.md` files.
`run .` opens Pi in that repo with the orchestrator router extension loaded.
For later sessions in the same repo, run only the `run .` command.

Mission goals can be planned into backlog DAGs only from explicit goal bullets
of the form `- ORCH-016: Task title`; `tools/mission.py plan-backlog <mission>`
creates missing planned backlog items, links all parsed items to the mission,
and leaves routing, GitHub, git branches, PRs, merge, deploy, and auto-merge
outside the planner boundary.

Once Pi opens, use your orchestration prompt/command with the engineering
request, for example:

```text
/orchestrate Audit this repo for security and correctness issues, then propose prioritized fixes.
/orchestrate Fix the metadata persistence bug and add regression tests.
```

If your Pi setup does not have `/orchestrate` registered as a slash command,
print the prompt and paste/use it in Pi with your request:

```sh
curl -fsSL "$ORCH_RAW" | bash -s -- prompt
```

Useful helper commands from any target repo:

```sh
curl -fsSL "$ORCH_RAW" | bash -s -- update      # refresh the cached orchestrator
curl -fsSL "$ORCH_RAW" | bash -s -- path        # print the cached repo path
curl -fsSL "$ORCH_RAW" | bash -s -- prompt      # print prompts/orchestrate.md
curl -fsSL "$ORCH_RAW" | bash -s -- run . -- --help
```

Pin a known release, branch, or commit instead of latest with
`PI_CODING_ORCHESTRATOR_REF`:

```sh
export PI_CODING_ORCHESTRATOR_REF=v1.2.3
curl -fsSL "$ORCH_RAW" | bash -s -- run existing-project
```

If this repository is already cloned locally, the same helper is available as
`scripts/use-latest.sh`, for example
`pi-coding-orchestrator/scripts/use-latest.sh run existing-project`. It refuses
to update a dirty cache or one whose Git origin differs from the configured
orchestrator URL.

### v1.0 readiness and reuse

For router-only reuse, load the extension and operational prompt from this
repository (or the helper's pinned cache) while Pi runs in the target project.
The extension requires the complete orchestrator tree; `use-latest.sh run .`
handles that path and passes any arguments after `--` to Pi. Use a reviewed
release/commit for repeatable setups rather than tracking the default branch.

Keep project-specific rules in the target project's `AGENTS.md` and
`.pi/routing.yaml`. The latter supports only additive constraints and risk
floors; it cannot replace routes, models, permissions, or global requirements.
Start from `templates/project-routing.yaml` or use `use-latest.sh init .` to
create conservative starter files. For a target project that needs durable,
project-owned backlog and provenance, vendor the full tree instead of using
router-only mode. Validate after setup with `python3 tools/validate_routing.py`,
`python3 tools/backlog.py validate`, `npm test`, `npm run typecheck`, and
`python3 -m pytest` (or the documented unittest command).

Release workflow and exit checks are in [`docs/release-checklist.md`](docs/release-checklist.md).
A sample persisted-run report below demonstrates the user-facing route,
execution-plan, agent tree, returned artifacts, and validation evidence; obtain
an actual report with `python3 tools/backlog.py trace --latest`.

### Install and run this repository directly

```sh
python3 -m pip install -r requirements-dev.txt
npm install
```

Try the adapter directly:

```sh
echo '{"classified_task": {"id": "t1", "goal": "...", "task_type": "debugging", "failure_cost": "high", "semantic_correctness": "normal", "ambiguity": 5, "implementation_scope": "small", "implementation_required": true, "cross_layer": false, "behavior_preservation": "required", "diagnosis_required": true, "assumptions_must_be_explicit": false, "many_routine_decisions": false, "production_feature": false, "security_sensitive": false, "destructive_operation": false, "escalation_evidence": {"failure_cost": "Lost edits destroy persisted user data.", "behavior_preservation": "Persisted behavior must not change except for the bug."}, "dependencies": [], "acceptance_criteria": ["..."], "files_or_areas": ["..."]}}' \
  | python3 tools/route_task.py
```

Load the extension in Pi with the full repository present alongside it:

```sh
pi -e extensions/model-router.ts
```

### How to validate (runtime)

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
npm test
npm run typecheck
```

`tests/test_runtime_routing.py` exercises `tools/routing_engine.py` directly
-- including primary-intent derivation, execution-plan materialization for
every route/intent combination, worker mutation-permission invariants, the
`CLAUDE_CODEX`/`CODEX_CLAUDE_CODEX` independent-review guarantees, and
bootstrap failure on a corrupted policy plan -- and `tools/route_task.py` as
a real subprocess, consuming `tests/routing-cases.yaml` directly so runtime
behavior can never drift from the static fixtures. `tests/model-router.test.ts`
exercises the extension's tool registration, its canonical (non-duplicated)
parameter schema, a real `route_task` call through the subprocess boundary,
and rejection of a fake adapter's `ok: true` response when its
`execution_plan` is missing or schema-invalid, using Node's built-in test
runner (`node --test`) — no new JavaScript YAML or JSON Schema dependency is
added (the extension's structural plan check reuses the `typebox` dependency
already used for its tool parameter schema); all *semantic* schema/YAML/
evaluator logic stays solely in `tools/routing_engine.py`.
`tests/test_delegation_policy.py` additionally checks delegation fan-out limits,
Claude tier cross-resolution, and policy bootstrap invariants.


## Backlog, run provenance, and GitHub synchronization (v0.3.5/v0.7)

Everything in this section is additive on top of the routing architecture
above: routing decides *how* a task is executed; the backlog and provenance
layer described here separately track *what* work exists, *what state* it is
in, and *what actually happened* when it was executed. None of it
duplicates or reinterprets `routing/default-policy.yaml`'s route predicates,
execution-plan semantics, or backend bindings — a run record stores a
`route_task` result verbatim (see "Run provenance" below).

### Using the orchestrator in an existing project with durable issue handling

There are two useful adoption modes:

1. **Router-only**: keep this repository cloned somewhere trusted and load
   `extensions/model-router.ts` into Pi while working inside another project.
   This gives the target project deterministic Claude/Codex routing, but the
   durable backlog/provenance files remain in this orchestrator repository.
2. **Durable project-local orchestration**: vendor or copy this repository's
   orchestration assets into the target project so that the target project owns
   its own `backlog/backlog.yaml`, `provenance/runs/`, schemas, routing policy,
   extension, and tools. Use this mode when GitHub Issues/PRs in the target
   repository should be synchronized with that target repository's backlog.

A quick bootstrap for router-only use in an existing project:

```sh
# Run from this orchestrator repository, pointing at the target repo.
TARGET_REPO=..$(printf /)existing-project
python3 tools/init_project.py "$TARGET_REPO"

# Or also copy the router extension into the target repo for local loading.
python3 tools/init_project.py "$TARGET_REPO" --copy-extension
```

This writes conservative starter files in the target repository:

- `.pi/routing.yaml` — project-local risk floors/invariants template;
- `AGENTS.md` — project instructions template;
- `.pi/orchestrator-pr-workflow.md` — explicit branch/PR/trace/issue-closure workflow.

Then run Pi from the target repository and load the router from this repository:

```sh
cd "$TARGET_REPO"
ORCHESTRATOR_REPO=..$(printf /)pi-coding-orchestrator
pi -e "$ORCHESTRATOR_REPO"$(printf /)extensions$(printf /)model-router.ts
# or, if --copy-extension was used:
pi -e .pi/extensions/model-router.ts
```

For durable project-local use, vendor or copy the full orchestrator tree into the target project so the target owns its own `tools/`, `schemas/`, `backlog/`, and `provenance/` directories. Prefer a reviewed subtree/submodule/vendor copy in real projects; this rsync example is only the mechanical shape:

```sh
cd existing-project
ORCHESTRATOR_REPO=..$(printf /)pi-coding-orchestrator
rsync -a --exclude .git --exclude node_modules \
  "$ORCHESTRATOR_REPO"$(printf /) ./

npm install
python3 -m pip install -r requirements-dev.txt
python3 tools/init_project.py . --copy-extension
python3 tools/validate_routing.py
python3 tools/backlog.py validate
python3 tools/backlog.py github-status
```

Then run Pi from the target repository and use `prompts/orchestrate.md` as the
operational prompt (for example as a custom `/orchestrate` prompt). The normal
issue lifecycle is:

```sh
# Create or import durable work items.
python3 tools/backlog.py add \
  --title "Implement export workflow" \
  --description "Build the end-to-end export workflow with tests." \
  --version v0.1 \
  --priority medium \
  --acceptance-criteria "Export works end to end" "Regression tests pass" \
  --labels feature export

# Create/update the corresponding GitHub issue.
python3 tools/backlog.py github-push ORCH-001

# Work only on items that are dependency-ready.
python3 tools/backlog.py next
python3 tools/backlog.py update ORCH-001 --status planned
python3 tools/backlog.py update ORCH-001 --status ready
python3 tools/backlog.py update ORCH-001 --status in_progress

# Orchestrate implementation/review in Pi, store run provenance, validate, then:
python3 tools/backlog.py update ORCH-001 --status review

# Open the PR with the gated publication path, then link it (no automatic closing).
python3 tools/backlog.py github-pr-link ORCH-001 17

# Only after deterministic acceptance criteria are satisfied:
python3 tools/backlog.py update ORCH-001 --status completed
python3 tools/backlog.py github-push ORCH-001  # close-guard permits closing now
```

`github-push` needs `create_github_issues: true` in `.pi/autonomy.yaml` on the
repository's default branch on GitHub whenever it would create, edit, close or
reopen an issue (#58); otherwise it stops with `GITHUB_WRITE_NOT_AUTHORIZED`
before writing anything.

Important safety limits are intentional: these tools do not deploy, merge PRs,
push branches, or allow workers to mutate GitHub. PR bodies generated by the
tool use `Refs #N`, not `Closes #N`, even when `.pi/autonomy.yaml` sets
`issue_reference: closes`; a backlog item's issue closes only via
`github-push` after the local backlog item is `completed`.

Four concerns, kept architecturally separate and never blended:

| Concern | Owner | Purpose |
|---|---|---|
| Human-facing backlog/collaboration | GitHub Issues | Where humans read, comment on, and track work. |
| Machine-readable backlog | `backlog/backlog.yaml` | Synchronized task metadata and the dependency graph. |
| Run provenance | `provenance/runs/<run_id>.json` | Append-only record of what an orchestration run actually did: routing decisions, delegated steps, worker/model attribution, outcomes, review findings, repairs. |
| Routing policy | `routing/default-policy.yaml` | Model/workflow decisions (unchanged by this section; see above). |

### Backlog storage: one collection file, not one-file-per-task

`backlog/backlog.yaml` is a single YAML list of backlog items, always
re-serialized sorted by `id`. This was chosen over one-file-per-task because
the backlog is small (tens, not thousands, of items), most operations need
to reason about the whole dependency graph at once (cycle detection, "what's
ready next", reporting), and a single sorted file keeps `git diff` reviewable
— each mutation touches one list entry, and stable id-sorted ordering means
unrelated items never move — without the directory-listing/merge overhead of
one-file-per-task. Run provenance, by contrast, genuinely is one-file-per-run
under `provenance/runs/`, because runs are independently created/finalized
append-only artifacts with no shared-file contention concern. Both choices
are documented in `tools/backlog_store.py`'s and `tools/provenance.py`'s
module docstrings.

Each backlog item is validated against `schemas/backlog-item.schema.json`
and has (at minimum) `id` (a stable `ORCH-NNN` identifier), `title`,
`description`, `status`, `version`, `priority`, `dependencies`,
`github_issue` (optional), `github_pr` (optional), `created_at`,
`updated_at`, `acceptance_criteria`, and optional `labels`. Additional
fields exist solely to make status transitions and GitHub conflict detection
deterministic rather than LLM-narrated: `blocked_from`/`blocked_reason` (why
an item is blocked, and what state to return to), `github_sync` (last known
issue title/state/labels snapshot), and `github_pr_sync` (last known PR
title/state/body/ref snapshot). Sync snapshots are used only for conflict
detection and are never authoritative for backlog status.

### Backlog statuses and transitions

Exactly eight statuses, no aliases: `proposed`, `planned`, `ready`,
`in_progress`, `review`, `blocked`, `completed`, `cancelled`. The
deterministic transition table (`tools/backlog_store.py`'s `TRANSITIONS`):

```
proposed    -> planned, cancelled
planned     -> proposed, ready, blocked, cancelled
ready       -> planned, in_progress, blocked, cancelled
in_progress -> ready, review, blocked, cancelled
review      -> in_progress, completed, blocked, cancelled
blocked     -> (the status it was in immediately before blocking), planned, cancelled
completed   -> (terminal; no outgoing transitions)
cancelled   -> (terminal; no outgoing transitions)
```

Enforced deterministically, never by narration:

- Entering `ready` or `completed` requires every dependency to already be
  `completed` (`DependencyNotCompletedError` otherwise).
- Entering `blocked` requires a non-empty `blocked_reason`; the prior status
  is recorded in `blocked_from` so `blocked` can return to it without
  guessing, and both fields are cleared on leaving `blocked`.
- The dependency graph is validated for dangling references and circular
  dependencies (`tools/backlog_store.py`'s `detect_cycle`) on every write.
- No-op transitions (same status to itself) and any transition out of a
  terminal status are rejected.

### Run provenance

**Parent lifecycle.** `agent_settled` ends one Pi agent activity, so it does
not finalize a parent run. The lifecycle extension records the active run ID,
Pi session ID, and target worktree in a private Pi session entry. Turns and
compaction retain that entry. Reload and resume of the same saved session
verify that the durable parent is still `IN_PROGRESS` and that the target
worktree still resolves to the recorded GitHub repository before restoring
it. Worker handles, worktree bindings, and backend approvals remain process
local and must be established again. Explicit `finish`, cancellation, a new
session, and quit close the parent. A crash leaves it resumable from the saved
session; a terminal record cannot be reopened.

**Single-owner run lease (audit A3).** `tools/run_lease.py` gives each parent
run one owner. The lease is a JSON file in orchestrator state beside the run
records (`<state>/leases/<run_id>.json`, or `provenance/leases/` for a
repository-local installation; never in the target checkout). It holds
`owner_id`, `pid`, `pid_start` (start time from `/proc/<pid>/stat`, so a reused
pid is not mistaken for the holder), `host` and `host_id` (a hash of the kernel
boot id, machine id and pid namespace, so containers that share a hostname
never judge each other's pids), `acquired_at`, `heartbeat_at`, `expires_at`,
`ttl_seconds` (default 300) and the lease's `takeovers`. Every decision is
made under an exclusive `flock` on `<run_id>.lock` with a bounded wait, and
every write is an atomic replace through `tools/atomic_io.py`. A crash
releases the flock with the process.

```sh
python3 tools/run_lease.py acquire   <run_id> --owner <id> [--pid N] [--ttl S] [--handoff]
python3 tools/run_lease.py heartbeat <run_id> --owner <id> [--pid N] [--ttl S]
python3 tools/run_lease.py release   <run_id> --owner <id>
python3 tools/run_lease.py status    <run_id>
python3 tools/run_lease.py takeover  <run_id> --owner <id> --reason <CODE> [--force-reason <CODE>] [--pid N] [--ttl S]
```

The CLI prints one JSON object and exits 0 (ok), 1 (refused) or 2 (invalid
invocation); `--pid` defaults to the calling process.

- `acquire` takes a free lease, or renews the caller's own (same owner id and
  same pid). The same owner from another pid is refused with
  `RUN_LEASE_PID_MISMATCH` unless it is the explicit hand-off (`--handoff`):
  the caller is the current holder handing it to a process it started, or the
  new process is a child of the holder. Another owner's live lease is refused
  with `RUN_LEASE_HELD`, a stale one with `RUN_LEASE_STALE`. Acquire never
  waits and never takes over.
- A lease is stale when its heartbeat expired (`HEARTBEAT_EXPIRED`), its pid
  is dead or reused on this machine (`OWNER_PROCESS_DEAD`), or the file is
  unreadable or implausible (`LEASE_UNREADABLE`: bad JSON, an expiry beyond
  the heartbeat plus the 24-hour maximum TTL, or a heartbeat more than 120
  seconds in the future). The holder's pid is probed on this machine for every
  lease that parses, implausible timestamps included, so a clock that moved
  backwards after sleep never makes a running holder look abandoned. A pid on
  another machine cannot be probed, so only the expiry counts there. Without
  `/proc` (macOS and other non-Linux hosts) the pid start time and machine key
  are unavailable: the hostname identifies the host, and a live pid counts as
  the holder. Hosts without a machine key that share one state directory over
  a network filesystem, under the same hostname, would therefore trust each
  other's pid probes; give each host its own state directory. On Windows no
  signal is ever sent (`os.kill(pid, 0)` would send CTRL_C_EVENT): a pid
  always counts as alive and only the expiry decides.
- Only `takeover` recovers a stale lease. It refuses a live lease
  (`RUN_LEASE_HELD`) or a missing one (`RUN_LEASE_NOT_HELD`). If the heartbeat
  expired but the holder process is still running on this machine, it is
  refused with `RUN_LEASE_HOLDER_ALIVE` unless a separate `--force-reason`
  explains why a running holder may be displaced. It first appends the
  previous holder, both reasons, `forced` and the new owner to the run's
  `parent_orchestration.lease_takeovers` and sets `parent_orchestration.owner_id`,
  and refuses when that cannot be recorded (`RUN_LEASE_PROVENANCE_UNAVAILABLE`,
  including a terminal run).
- `heartbeat` and `release` act only on the caller's own lease (`RUN_LEASE_LOST`
  when another owner holds it, `RUN_LEASE_NOT_HELD` when there is none,
  `RUN_LEASE_PID_MISMATCH` for a heartbeat from another pid). Other codes:
  `RUN_LEASE_INVALID`, `RUN_LEASE_LOCK_TIMEOUT`, `RUN_LEASE_STORAGE_UNAVAILABLE`.

**Owned writes.** `parent_orchestration.owner_id` records who owns a run: the
owner that started it, a verified resume, or a takeover. Every write to the
run (`event`, `finish`, `settle`, `context_budget.py start|checkpoint`) takes
`--owner`, which the CLI requires. Under the lease lock, and before the run
lock (always in that order), the write is refused unless the caller holds the
lease, live or stale but never taken over, or the lease is free and the
caller is the recorded owner (`RUN_LEASE_LOST`, `RUN_LEASE_NOT_HELD`). An
explicit outcome releases the caller's lease inside the same lock; there is
no owner-blind release. A process that slept past its expiry while another
owner took the run over therefore can no longer settle, finish or write to
that run, nor free the new owner's lease. Runs created in-process without an
owner, and records from before leases, have no recorded owner and accept any
writer while no lease exists; `ParentRun` gives its in-process run an owner.
Such runs (`owner_id` absent or null: records from before leases, or created
through the Python API without an owner) have no single-owner protection, so
the queue dispatcher (#34) must refuse to operate on them. A verified resume
by another owner changes `owner_id` and records the previous owner as
`previous_owner_id` on that `resume_checks` entry.

**Threat model.** The owner id and the hand-off rule protect a run against
stale, sleeping, duplicated or buggy processes of the same user, not against a
hostile local user. The owner id is not a secret: it is in the lease file, in
the run record and in Garden. In the Python API `caller_pid` is supplied by
the caller, and anyone who can write the state directory can edit the lease.
Isolation between local users relies on file permissions on the state
directory.

**Wiring.** `parent_orchestration.py start --owner <id> --pid <pid>` records the
owner and takes the lease of the new run. `parent_orchestration.py resume
<run_id> --worktree <path> --owner <id> --pid <pid> [--handoff]` verifies the
target identity, takes the lease, re-reads the run under the run lock (a run
that finished meanwhile is returned as it is and the lease released) and
verifies the resume checkpoint. The lifecycle extension calls `start` for a
new run and `resume` (not `status`) when it restores a saved session. Its
owner id is the launcher's `PI_ORCHESTRATION_LEASE_OWNER`, which it reads once
and removes from the environment so nested processes never inherit it, and
then resumes with `--handoff`. Otherwise it is `pi-<host>-<pid>-<random
per-process nonce>-<session id>`, stable across an extension reload in the
same process. It renews the lease every turn and every 60 seconds and
remembers the last successful renewal. A heartbeat answering `RUN_LEASE_LOST`,
`RUN_LEASE_NOT_HELD` or `RUN_LEASE_PID_MISMATCH`, or a write refused for the
lease, detaches the process from the run. So does any run of failures
(including lock timeouts, storage errors or a subprocess timeout) that
reaches the lease lifetime minus a 60-second margin (`RUN_LEASE_SELF_EXPIRED`).
Detaching blocks every tool, aborts running ClaudeWorker processes and the
current agent operation (which stops in-flight subagents), and never
settles, finishes or replaces the other owner's run. A refused restore
behaves the same way. The lease guards who may continue and write a run; it
does not add idempotency keys at GitHub or other external mutation
boundaries.

**Resume checkpoint (audit A10).** `parent_orchestration.checkpoint` holds the
parent checkout's resolved `worktree`, `branch` (null when detached), `head`,
the `phase`, a `reason` and a `sequence`. It is written at run start and
refreshed at every phase event; at worker approval (the lifecycle sends a
`checkpoint` event after a successful route preflight and after
`bind_workspace`); when a wave starts integrating, before integration changes
the parent checkout; when it is integrated; and when a mutation count is
recorded. A repeated event on an unchanged checkout leaves the record
byte-identical. `resume` compares worktree, branch and HEAD with the live
checkout while holding the lease and appends the result to
`parent_orchestration.resume_checks`. Any difference refuses the resume with
`RESUME_CHECKPOINT_DRIFT` and the expected and actual values in `details`. It
releases the lease just taken and leaves the run `IN_PROGRESS` with its
recorded owner and checkpoint unchanged: nothing is re-based or closed. An
operator either restores the checkout and resumes again, or takes the free
lease (`run_lease.py acquire`) and records an explicit outcome with
`parent_orchestration.py finish --owner`. A crash in the middle of
integration therefore shows up as drift against the pre-integration
checkpoint. A record from before checkpoints existed still loads, reports
status and accepts events, but cannot be verified: its resume is refused with
`RESUME_CHECKPOINT_MISSING` (recorded as `unverified`) and it stays
`IN_PROGRESS` for an operator. Working-tree changes that were never committed
are not part of the comparison. Coverage: `tests/test_run_lease.py`,
`tests/orchestration-lifecycle.test.ts`, `tests/external-cwd-lifecycle.test.ts`
and `tests/test_headless_run.py`.

**Open work at the end of a run (issue #42).** A run that finishes `PARTIAL`,
`BLOCKED`, `FAILED` or is settled leaves nothing `not_started` or
`in_progress`. `parent_orchestration.py finish` sets each such phase, and each
worker of the phase's current route epoch, to the status `abandoned` and
appends one entry to `parent_orchestration.reconciled`: `kind` (`phase` or
`worker`), `phase_id`, `step_id` and `route_epoch` (workers), `from_status`
(`not_started` or `in_progress`), `to` (`abandoned`), `reason` (`run finished
<STATUS>`) and `at`. Events, activity, escalations and every other recorded
field are unchanged, so the prior status is always recoverable from
`reconciled`. Workers of an earlier route epoch were superseded by an
escalation and, as in the `COMPLETED` check, are left as recorded. A finish on
a terminal run changes nothing, and a record that ended with nothing open has
no `reconciled` field. `COMPLETED` still refuses any open phase or current-epoch
worker. `abandoned` is only ever written by the finish; an event cannot set it,
and it is unrelated to the headless execution state `interrupted`. The Garden
shows it as "Abandoned (run ended PARTIAL)". Records from before this change
validate and display unchanged; older PICO versions reject `reconciled` and
`abandoned` (`additionalProperties: false`, status enums).

**ClaudeWorker results.** When a ClaudeWorker result arrives the lifecycle
extension records the worker's raw outcome with the lease owner: `completed`
on success, `failed` with a blocker code on error, `blocked` on abort. The
event carries `only_if_open`, so it never overrides a status the lead already
recorded. The lead's judgment wins: it may move a `completed` worker to
`failed` or `blocked` by recording that status with a `blocker_code`, and may
restate a `failed` or `blocked` worker with its own `blocker_code`, which
replaces the automatic one; `failed` may likewise be reclassified `blocked`
with a `blocker_code`. Other changes of a terminal worker stay refused. No
override rewrites history: each is appended to
`parent_orchestration.worker_overrides` as `phase_id`, `step_id`,
`route_epoch`, `from_status`, `from_blocker_code`, `from_details?` (backend
versions), `to_status`, `blocker_code`, `phase_status`, `by: "lead"` and `at`,
while the worker holds the current values. An override inside a phase that is
already terminal is allowed (a phase cannot be reopened, and the lead's
judgment must be able to win) and is flagged by `phase_status` being that
terminal value. The list is bounded: recoding a worker to a blocker code it
already had in that route epoch is refused with
`PARENT_WORKER_RECODE_REPEATED`, a worker takes at most 8 overrides, and a run
at most 200 (`PARENT_WORKER_OVERRIDE_LIMIT`; the schema carries the same
`maxItems`). These refusals change nothing and the lifecycle extension reports
them to the lead without blocking the run. Older PICO versions reject
`worker_overrides`.
Coverage: `tests/test_reconcile_open.py`, `tests/orchestration-lifecycle.test.ts`.

**Ownership.** Run records belong to the orchestrator installation whose
`tools/` write them (`ROOT/provenance/runs/`). That holds even when Pi runs
from an external target repository. The target is identified inside the record
(`parent_orchestration.target_repository`) and is never written to. Records
stay installation-owned because parent runs link GitHub-adapter child runs that
must live in the same store, because traces in the target would dirty the
user's worktree, and because records are validated against the installation's
own schema. A vendored copy of the full tree is its own installation, so it
owns its records. `scripts/use-latest.sh` preserves generated `run-*.json`
files across cache updates and refuses to update if upstream would overwrite
one. Historical records are never migrated or deleted. Coverage:
`tests/external-cwd-lifecycle.test.ts` and
`tests/test_launcher_preflight_chain.py`.

`provenance/runs/<run_id>.json`, validated against
`schemas/run-record.schema.json`, records one orchestration run: `run_id`,
`backlog_id`, `github_issue`, `user_objective`, `started_at`/`ended_at`,
`route_task_result` (the `tools/route_task.py` result stored byte-for-byte
verbatim — this module never reinterprets routing), observational
`routing_telemetry`, `starting_commit`/`ending_commit`, an overall `outcome`,
an optional `orchestration_metrics` object for delegation-efficiency
evaluation, and a list of `steps`. Each step records `task_id`, `parent_task_id`,
`execution_plan_step_id`, `role`, `family`, `backend`, `model`,
`capacity_tier`, `reasoning_setting`, `context_strategy`,
`context_window_tier`, `fresh_context`, `mutation_permission`,
`worker_identifier`, `status`, `outcome`, `files_changed`,
`validation_performed`, `review_findings`, `repair_of`, `returned_artifacts`,
and per-step `telemetry`. Telemetry is a closed counters/timing object
(duration, bytes, tokens/cost if exposed) stored only in provenance; it is
never part of `route_task` results or routing policy decisions and must not
contain prompts, transcripts, hidden reasoning, or secrets. Every
attribution/telemetry field the orchestrator cannot observe is stored as an
explicit `null` (never a guessed value) — `tools/provenance.py`'s `add_step`
defaults every unspecified field to `null` rather than omitting it.

`validation_performed` items may be a plain command string (legacy/simple
form, rendered with an `unknown` result) or a structured object with
`command`, `result` (`pass`/`fail`/`partial`/`not_run`/`unknown`),
`exit_code`, and `summary`. `returned_artifacts` is an optional list of
compact artifact summaries a step actually returned — each entry has `kind`
(`specification`/`implementation`/`review`/`validation`/`documentation`/
`other`), a short `summary`, and a `reference` (a repository-relative path or
stable external reference); it never stores artifact bodies, diffs, stdout,
or transcripts. Both fields, and the top-level `trace_schema_version` marker,
are additive and intentionally absent from older run records' `required`
shape, so historical files remain valid and are rendered tolerantly rather
than migrated.

Every free-text field the orchestrator/a worker actually controls the
content of — not just `validation_performed`/`returned_artifacts`, but also
the top-level `user_objective`/`starting_commit`/`ending_commit` and each
step's `outcome`, `model`, `backend`, `worker_identifier`, and
`review_findings[].description` — must be a compact single-line string: no
embedded newlines or other control characters, and no recognizable
secret-like content (private-key headers, GitHub/AWS/Slack tokens, bearer
credentials). `returned_artifacts[].reference` must additionally be a
repository-relative path that stays inside the repository (no `../` escape
above the root) or a stable `http(s)` external reference, never an absolute
filesystem path. Fields whose value is already constrained to a closed enum
by the schema (`role`, `family`, `status`, `capacity_tier`, etc.) are exempt
— only genuinely free-text fields are checked. These content-level checks
are enforced only at write time (`tools/provenance.py`'s
`_validate_free_text_safety`), so a historical record predating this check
can still exist on disk; `render_trace_text` defensively escapes any stray
control/newline character it encounters — including inside nested
`classification`/`project_constraints`/capacity-assignment structures copied
from `route_task_result` — rather than ever emitting it verbatim (see
"Orchestration trace" below).

### Orchestration trace: how a run's agentic organization is reported

`tools/provenance.py` derives a deterministic, read-only **trace** from one
run record — the concrete answer to "how did the orchestrator decide, which
agents/subagents ran, what did they return, and what was the outcome" —
without re-evaluating routing, invoking `gh`, or guessing anything not
already persisted:

- `select_trace_runs(repo_root, *, run_id=None, backlog_id=None, latest=False)`
  — selects run record(s); `run_id` is exclusive with `backlog_id`/`latest`.
  Selection and `--latest` sort by each run's actual `started_at` instant
  (parsed as timezone-aware UTC), not by lexical string comparison — two
  valid RFC3339 timestamps with different UTC offsets can disagree
  lexically even though their instants are correctly ordered. A record with
  a missing/unparseable timestamp sorts as the oldest rather than being
  guessed or dropped.
- `build_trace_report(repo_root, record)` — derives a structured report:
  `trace_path`, backlog/GitHub references, `execution_plan` (copied from
  `route_task_result` verbatim, never reconstructed), and a `route` decision
  summary that retains every safe field from that same stored result —
  `ok`, `policy_version`, `selected_route`, `fallback_applied`, base/
  effective `matches`, `classification`, `capacity_assignments`,
  `backend_bindings`, `project_constraints`, and (for a failed `route_task`
  call) `errors` — so the trace can never silently hide the actual
  capacity/model decision. It also derives the `agents` tree (built solely
  from `task_id`/`parent_task_id`, in append order — see below), flattened
  `returned_artifacts` and `validation_results`, `routing_telemetry`/
  `orchestration_metrics`, and `warnings` for anything structurally odd.
- `render_trace_text(report)` — fixed-section plain text with no wall-clock-
  dependent content: identical reports always render identically. Every
  unobserved value prints as the literal string `unknown`; an empty
  collection prints as `(none observed)`, never a blank section. The `Route:`
  section prints a concise base/effective `Classification` summary (a
  fixed, risk-relevant field subset — the complete classification remains
  available verbatim via `--json`). The execution-plan section shows, per
  step, both the authored capacity tier and the actually *assigned* one as
  `capacity=<authored>-><assigned>` (a rule such as `ops-light`
  can downgrade an authored `standard` step to `light`), which `rule` (if
  any) made that decision, and the resolved `backend`/`model`/
  `context_window` — not just the authored tier in isolation, which could
  misrepresent what actually ran or hide the decision evidence. Every
  free-text value — including strings nested inside `classification`/
  `project_constraints`/capacity-assignment structures — is defensively
  sanitized (control/newline characters escaped) before being emitted, so a
  historical record predating `_validate_free_text_safety` can never inject
  a fake section into this fixed-section layout. Each `Agents:` entry also
  prints a concise `telemetry:` line (`source=... duration_ms=...
  `tokens(...)` with slash-separated input, output, cached, and total values) derived from that step's stored
  `telemetry`, or the literal `unknown` when no telemetry was recorded for
  that step; the `Telemetry:` section's `Routing:` line renders
  `routing_telemetry` the same way, while `Run metrics:` still prints the
  full `orchestration_metrics` object (its fields are already individually
  named/self-describing).

`tools/provenance.py` also exposes the observation helpers used to build
these `telemetry` objects in the first place: `start_observation()` captures
a monotonic clock reading (for duration, immune to system-clock adjustment)
and the wall-clock UTC `started_at` actually recorded; `finish_observation(
handle, *, source=None, usage=None, input_bytes=None, output_bytes=None)`
computes `duration_ms` from the elapsed monotonic time, stamps `ended_at`,
and safely extracts `input_tokens`/`output_tokens`/`cached_tokens`/
`total_tokens` via `extract_token_usage` — never estimating `queue_ms` or
`estimated_cost_usd`. `extract_token_usage(usage_source)` reads only
structured usage metadata: the object itself, or one wrapped under a
`usage`/`usage_metadata`/`token_usage` key, recognizing `input_tokens`/
`output_tokens`/`cached_tokens`/`total_tokens`, their camelCase equivalents,
`prompt_tokens`/`completion_tokens`, cache-read aliases, and Gemini's
`promptTokenCount`/`candidatesTokenCount`/`totalTokenCount`. Only
nonnegative integers (or integral floats) are accepted; an absent field stays
`None`, and a field observed under two disagreeing recognized names — or
holding a malformed value — also fails closed to `None` rather than guessing.
The raw usage object is never returned or persisted, only the extracted
integers. Both helpers return plain dicts suitable for `routing_telemetry=`/
`telemetry=` on `start_run`/`add_step`/`update_step`, which fill every
remaining unobserved field with explicit `null` via `_normalize_telemetry`.

The agent tree tolerates any malformed historical `parent_task_id` graph
(one written before `_validate_step_hierarchy` existed, or edited by hand)
without ever silently dropping a step: a step whose parent doesn't resolve
to an *already-established, unambiguous* earlier step — a forward reference,
a reference to a step that never appears, a self-reference, or a reference
to a `task_id` that appears more than once — is rendered as its own root
with a warning explaining why, rather than mis-attached or discarded. Once a
`task_id` has appeared more than once it becomes permanently ambiguous: every
occurrence from the duplicate onward renders as its own root, and any
*subsequent* step naming that id as `parent_task_id` also renders as a root
(since which of the same-id steps it meant can no longer be determined) —
but a child that named it as parent *before* the duplicate appeared keeps
its original attachment, since append order is never rewritten
retroactively. This also makes a genuine cycle impossible to render as one:
whichever step in append order names the other as its parent first loses
(becomes a root, with a warning) since its parent isn't established yet, and
the tree that results is always acyclic.

Exposed via `tools/backlog.py trace`:

```sh
python3 tools/backlog.py trace <run-id>
python3 tools/backlog.py trace --latest
python3 tools/backlog.py trace --backlog-id ORCH-011 [--latest]
python3 tools/backlog.py trace <run-id> --json   # {"schema_version": 1, "runs": [...]}
```

`RUN_ID` is mutually exclusive with `--backlog-id`/`--latest`; with no
selector or no matching run, the command fails with a concise error rather
than guessing. In day-to-day use, prefer `trace --latest` for the most recent
run, `trace --backlog-id ORCH-NNN --latest` for the latest run tied to a
specific backlog item, and `trace <run-id>` when a final report already gave
you the exact `provenance/runs/<run-id>.json` path. Metrics and token/cost
fields are observational only: they appear when a backend persisted them, and
otherwise render as `unknown`/`null` rather than being estimated.

Sample text output for a `CODEX_CLAUDE_CODEX` run (illustrative: the run and
backlog identifiers come from development before the public repository, and
no GitHub issue was recorded for this run):

```text
Run: run-2026-09-23-orch011
Trace: provenance/runs/run-2026-09-23-orch011.json
Status: completed
References: backlog ORCH-011; GitHub issue unknown
Objective: Implement end-of-run orchestration trace and user-facing summary.

Route:
  Selected: CODEX_CLAUDE_CODEX
  Policy version: 4
  Fallback applied: False
  Matches (base): ['CODEX', 'CODEX_CLAUDE_CODEX']
  Matches (effective): ['CODEX', 'CODEX_CLAUDE_CODEX']
  Classification (base): {'task_type': 'broad_feature', 'failure_cost': 'medium', 'semantic_correctness': 'critical', 'ambiguity': 5, 'implementation_scope': 'broad', 'behavior_preservation': 'required', 'diagnosis_required': False, 'assumptions_must_be_explicit': True, 'security_sensitive': False, 'destructive_operation': False}
  Classification (effective): {'task_type': 'broad_feature', 'failure_cost': 'medium', 'semantic_correctness': 'critical', 'ambiguity': 5, 'implementation_scope': 'broad', 'behavior_preservation': 'required', 'diagnosis_required': False, 'assumptions_must_be_explicit': True, 'security_sensitive': False, 'destructive_operation': False}

Execution plan:
  1. codex_specifier - specifier/codex intent=analysis capacity=high->high rule=unknown reasoning=high backend=spawn_subagent model=openai-codex/gpt-6.1-sol context_window=extended
  2. claude_worker - worker/claude intent=implementation capacity=high->high rule=unknown reasoning=high backend=ClaudeWorker model=anthropic/claude-opus-5-5 context_window=extended
  3. codex_reviewer - reviewer/codex intent=review capacity=high->high rule=unknown reasoning=high backend=spawn_subagent model=openai-codex/gpt-6.1-sol context_window=extended

Agents:
  - codex_specifier [specifier/codex] status=completed outcome=accepted
    telemetry: unknown
  - claude_worker [worker/claude] status=completed outcome=accepted
    telemetry: unknown
  - codex_reviewer [reviewer/codex] status=completed outcome=no required findings
    telemetry: unknown

Artifacts:
  - codex_specifier / specification: Trace data model and CLI contract (unknown)
  - claude_worker / implementation: provenance.py trace functions + backlog.py trace command (provenance/runs/)

Validation:
  - claude_worker: PASS [exit 0] `python3 -m pytest` - 228 passed

Telemetry:
  Routing: unknown
  Run metrics: unknown

Warnings: (none)

Final status: completed
```

`rule=unknown` here means no capacity rule matched this authored `high` step
(only an authored `standard` worker step can ever be downgraded); when a rule
does apply (e.g. `low-risk-worker` downgrading a step to `economy`),
`capacity=standard->economy` and `rule=low-risk-worker` are both shown.

If the run's `route_task_result` has `ok: false`, the `Route:` section
instead prints `Route selection failed (ok: false)` followed by each
`errors[].path`/`.message` pair, rather than an execution plan that never
existed.

This trace is safe to show a user: attribution/telemetry structures are
closed, `additionalProperties: false` objects that reject forbidden
secret/transcript-like *keys* (see `_validate_telemetry_keys`), and the
specific free-text fields a worker actually controls the content of
(`validation_performed`/`returned_artifacts` commands/summaries/references)
are additionally validated at write time for compact single-line content
and secret-like *values* (see `_validate_free_text_safety` above).
`render_trace_text` also defensively sanitizes any stray control/newline
character from a historical record that predates that check, so stored
content can never be rendered as a fake section header.

### Backlog and provenance commands

An LLM/orchestrator should never need to hand-edit `backlog/backlog.yaml` or
`provenance/runs/*.json`; use `tools/backlog.py`:

```sh
python3 tools/backlog.py list [--status STATUS] [--version VERSION]
python3 tools/backlog.py get <id>
python3 tools/backlog.py add --title ... --description ... --version ... [--id ORCH-NNN] [--priority ...] [--dependencies ...] [--acceptance-criteria ...] [--labels ...]
python3 tools/backlog.py update <id> [--status ...] [--blocked-reason ...] [--dependencies ...] ...
python3 tools/backlog.py next
python3 tools/backlog.py report [--json]
python3 tools/backlog.py trace <run-id> | --latest | --backlog-id <id> [--latest] [--json]
python3 tools/backlog.py validate
python3 tools/backlog.py github-status
python3 tools/backlog.py github-sync <id>
python3 tools/backlog.py github-push <id>
python3 tools/backlog.py github-pull <issue-number>
python3 tools/backlog.py github-pr-sync <id>
python3 tools/backlog.py github-pr-link <id> <pr-number>
```

`report` answers, deterministically from persisted state (never from
conversation history): what's in progress, what's ready next, what's blocked
and why, what was completed per version, which GitHub issue corresponds to
each item, and — by joining `provenance/runs/` — which worker/model handled
each completed execution step and what review findings/repairs occurred.

### Workspace execution plan: read-only multi-repository planning

`schemas/workspace.schema.json` (`schema_version: 1`) describes one mission
spanning several repositories. It is a separate record above the
single-repository `Mission`: mission records, `parent_orchestration.target_repository`
and the `SESSION_REPOSITORY_MISMATCH` / worktree scope checks are unchanged.
Each `repositories[]` entry requires an explicit `owner/repository`, a checkout
`path` (relative to the manifest) and a 40-character `pinned_commit`; a branch
name is not a pin. Each `tasks[]` entry names its owning repository id,
`depends_on` task ids and an optional `readiness` (default `planned`).
Optional `max_duration_seconds` (positive integer) on the manifest sets a
default wall-clock limit for every child execution and on a task overrides that
default; omitted everywhere means no limit (see "Execution time limit" under
"Headless single-repository execution"). It is unrelated to a check's
`timeout_seconds`. Manifests without the field are valid and behave as before.

```sh
python3 tools/workspace_plan.py workspace.yaml [--json] [--no-access-check]
pico plan workspace.yaml
```

The planner is read-only. Its only subprocesses are `git rev-parse`,
`git remote`, `git cat-file -e` and `gh repo view`. Per repository it calls
`github_integration.resolve_target` with the explicit identity, so the existing
`TARGET_REPOSITORY_UNRESOLVED` / `TARGET_REPOSITORY_CONFLICT` /
`TARGET_REPOSITORY_AMBIGUOUS` codes apply unchanged, then checks the Git common
directory and GitHub identity are unique across entries
(`WORKSPACE_REPOSITORY_DUPLICATE`), that the pinned commit exists in the
checkout (`WORKSPACE_PIN_MISSING`), and that the authenticated `gh` user can
read the repository (`WORKSPACE_REPOSITORY_ACCESS_DENIED`;
`GITHUB_ACCESS_UNVERIFIED` when `gh` is unavailable or unauthenticated).
Tasks are validated as a general DAG: `WORKSPACE_TASK_DUPLICATE`,
`WORKSPACE_TASK_REPOSITORY_UNKNOWN`, `WORKSPACE_DEPENDENCY_UNKNOWN`,
`WORKSPACE_DEPENDENCY_SELF`, `WORKSPACE_DEPENDENCY_CYCLE`. Waves are
topological levels (a task's wave is one more than its highest dependency),
ordered by task id, so the same manifest always yields the same plan.

Readiness states are distinct and declared in the manifest, never inferred:
`planned`, `implemented` (a PR exists), `validated` (its tests pass),
`ready_for_downstream` (the interface is declared stable for consumers) and
`merged`. Only `ready_for_downstream` and `merged` satisfy a dependent; a PR
existing or passing tests is not evidence that an interface is stable. For each
task the planner emits `readiness`, `blocked_by` (the dependency ids whose
readiness is neither `ready_for_downstream` nor `merged`, sorted) and
`dispatchable` (the task is still `planned`, lies on no cycle and `blocked_by`
is empty). A `summary` block gives the task count, a count per readiness state
and the sorted ids of dispatchable tasks. Repository blockers do not change
`dispatchable`; they are reported separately and make the plan `BLOCKED`.

The output records the immutable execution inputs — resolved repository
identity, Git common directory, pinned commit and orchestrator revision — the
tasks with `depends_on`, `blocks`, `wave`, `readiness`, `blocked_by` and
`dispatchable`, the waves, the `summary`, every blocker, and a
`mutations` block that is always zero. Exit status is 0 for `PLANNED`, 1 for
`BLOCKED`, 2 for an unreadable or schema-invalid manifest
(`WORKSPACE_MANIFEST_INVALID`). Blockers never stop validation of the
remaining entries, so one report lists everything to fix. Execution of a plan
(headless child runs, scheduling, contracts, PR coordination, Garden) is
described in the sections that follow.

### Headless single-repository execution

`tools/headless_run.py` (launcher: `pico exec`) starts, observes, resumes and
cancels one bounded orchestration run without an interactive terminal. It is
the programmatic execution interface that repository-scoped child launchers
and the cross-repository scheduler build on.

```sh
python3 tools/headless_run.py start  --target . --repo owner/repository --execution-id <id> --objective "..." [--allow-mutation] [--resume [--takeover-stale-lease]] [--no-wait] [--max-duration SECONDS]
python3 tools/headless_run.py status --target . --repo owner/repository --execution-id <id>
python3 tools/headless_run.py cancel --target . --repo owner/repository --execution-id <id>
```

**Mechanism.** `start` resolves the target through
`github_integration.resolve_target` with the explicit identity (so the
existing `TARGET_REPOSITORY_*` codes apply), then launches
`pi --print --mode json --session-id <uuid> --session-dir <state>/executions/<repo-key>/sessions`
with the four orchestrator extensions and the `/orchestrate` prompt built from
`prompts/orchestrate-core.md` (frontmatter stripped, `$ARGUMENTS` replaced).
The environment carries `PI_TARGET_REPOSITORY`, the pinned Claude backend
identity, and `PI_ORCHESTRATOR_STATE_DIR`, exactly as `pico run` sets them.
The orchestration-lifecycle extension therefore creates the parent run, keeps
every guard, and settles the run as `BLOCKED` /
`ORCHESTRATION_OUTCOME_UNRECORDED` when the process exits without `finish`.
The parent run id is discovered from the lifecycle's
`pi-orchestration-parent-v1` session entry; nothing is written to the target.

**Idempotency and recovery.** Execution records live at
`<state>/executions/<repo-key>/<execution-id>.json`, keyed by the target's Git
common directory. Statuses: `launched` (process alive), `exited` (process
ended and the run is terminal), `interrupted` (process gone while the run is
still `IN_PROGRESS`, i.e. a crash or kill), `cancelled`. A repeated `start`
returns `already_running`, `already_finished` or `resume_required` without
launching anything. `--resume` relaunches an interrupted execution with the
same Pi session id, so the lifecycle's `session_start` restore path verifies
the run is still `IN_PROGRESS` and the worktree still resolves to the recorded
repository before continuing it; a terminal run is never resurrected
(`already_finished`). `cancel` sends SIGTERM to the process group, escalates
to SIGKILL after a grace period, and finishes the parent run `BLOCKED` with
`EXECUTION_CANCELLED` (a no-op on an already terminal run). An execution id
is bound to one repository: using it from another checkout fails with
`EXECUTION_UNKNOWN` or `EXECUTION_TARGET_MISMATCH`.

**Run lease.** Each launch gets a fresh lease owner id, stored as the
execution's `lease_owner` and passed to Pi as `PI_ORCHESTRATION_LEASE_OWNER`.
The launched lifecycle therefore takes the run lease under it, and removes
the variable from its environment. A `--resume` claims the lease before
launching anything, then hands it to the child's pid (`handoff`). If the
hand-off fails, the child is stopped and the execution records
`lease_handoff_error` (`RUN_LEASE_HANDOFF_FAILED`), so nothing runs under a
lease it does not hold. A live lease of another process refuses the resume
with `RUN_LEASE_HELD`. A crashed launch leaves a stale lease (its pid is dead),
so `--resume` alone fails with `RUN_LEASE_STALE`. `--resume
--takeover-stale-lease` is the explicit recovery, recorded in the run's
`lease_takeovers` with reason `HEADLESS_RESUME_TAKEOVER`. It never forces: a
holder still running on this machine is refused with `RUN_LEASE_HOLDER_ALIVE`.
`pico workspace launch --resume --takeover-stale-lease` passes it through, and
the scheduler's explicit `--resume-interrupted` implies it, never with a force
reason. The launched lifecycle then verifies the resume checkpoint, so a
checkout that moved since the crash refuses the resume with
`RESUME_CHECKPOINT_DRIFT` and leaves the run `IN_PROGRESS`. `cancel` closes the
run as the execution's lease owner; a run another owner has taken over is not
closed (`cancel_error_code`). The execution records the child's pid start time; `status`, `cancel` and the workspace runner treat a live pid with another start time as a gone child (`child_gone_reason: PID_REUSED`), and `cancel` never signals it. `status` and the execution's `run` block show
the lease holder (`run.lease`: state, stale reason, holder alive, owner, pid,
host, heartbeat, expiry), as does Garden for each run.

**Execution time limit.** `start --max-duration SECONDS` (`max_duration_seconds`,
a positive integer; anything else is refused with `MAX_DURATION_INVALID` before
a child exists) limits the wall-clock time of the child. The launch records
`max_duration_seconds`, `deadline_at` (UTC, for people) and `deadline_epoch`
(what the code compares). Without the flag nothing is recorded and nothing
changes. The limit is enforced by the code that already looks at the child, not
by a watchdog process: a launcher that waits slices its wait and checks the
deadline, and `status` (hence `pico workspace status`, the scheduler's
observation of children it did not start) checks it
on every refresh. Both call the same stop path as `cancel`: the parent run is
finished `BLOCKED` with `EXECUTION_TIME_LIMIT` as the lease owner (so a run
another owner has taken over is not closed), then the child's process group
gets SIGTERM and, after the grace period, SIGKILL, only while the pid and its
recorded start time still match (`execution_alive`; a reused pid is never
signalled). **Clock.** Where `/proc` is available, elapsed time is the boot-clock
time since the child's recorded `pid_start` (against `/proc/uptime`), so a
wall-clock jump (NTP step, WSL2 resuming from host sleep) does not stop a child
early or late. Elsewhere it falls back to the wall clock (`deadline_epoch`), which
can then be off by such a jump; displays computed from the workspace record
always use `deadline_epoch`. A killed child records its real signal status
as `exit_code` (negative), never 0. The execution ends `status: cancelled`, `outcome: time_limit`,
`stop_reason: EXECUTION_TIME_LIMIT` with a `time_limit` block
(`max_duration_seconds`, `elapsed_seconds`, `deadline_at`, `stopped_at`). A
child that ended on its own before the check keeps its own outcome, and the
limit is not claimed for it. Limits of a `--no-wait` child are only enforced
when something looks at it (`status`, the scheduler, or a
later `start` of the same id): there is deliberately no background process, so
a detached child nobody observes keeps running until someone does. A child
stopped before it created its parent run has no run to settle; the stop still
ends the execution `cancelled` with the reason. **Resume and the limit.** A
time-limit stop closes the run `BLOCKED`, a terminal state, so `start` and
`--resume` of that execution id return `already_finished` and launch nothing;
continuing the work is an explicit new launch with a new execution id (a new
run). The limit is per launch and is not inherited: an interrupted (crashed)
execution resumed with `--resume --max-duration N` gets a fresh N-second window
starting at the resume, and a resume without `--max-duration` runs unlimited.

**Lead-call limit (`max_lead_calls`).** Optional, a positive integer (`MAX_LEAD_CALLS_INVALID`
otherwise). Sources and precedence: `pico exec start --max-lead-calls N` or the workspace
task's `max_lead_calls` (source `cli` / `task`), then the manifest's `max_lead_calls`
(`manifest`), then `limits.max_lead_calls` in the target's `.pi/autonomy.yaml` (`policy`);
none set means no limit and unchanged behavior. `lead_calls_warn_at` follows the same levels
(`--lead-calls-warn-at`, task, manifest, `limits.lead_calls_warn_at`), must be below the limit
(`LEAD_CALLS_WARN_AT_INVALID`) and defaults to 80% of the limit rounded down (no default
warning for a limit of 1); a repository threshold at or above a lower explicit limit is not
inherited. The launcher resolves the effective values, records them in the execution record
(`max_lead_calls`, `max_lead_calls_source`, `lead_calls_warn_at`) and hands them to the child as
`PI_ORCHESTRATION_MAX_LEAD_CALLS`, `PI_ORCHESTRATION_MAX_LEAD_CALLS_SOURCE` and
`PI_ORCHESTRATION_LEAD_CALLS_WARN_AT`; an interactive session without the launcher reads the
policy itself. The lifecycle extension records `context.lead_call_limit` (limit, source,
`warn_at`, `warned_at_call`, `calls_at_limit`, `stopped`, `stopped_reason`, `calls_at_stop`).
The count is the larger of the recorded `lead_calls` and the extension's own count (`calls_seen`, carried by its limit events, so a lost telemetry write cannot undercount); it carries over a resume, and a restored session re-applies the effective limit at `session_start`. Every assistant call counts, including one whose model string fails validation.

Enforcement is deterministic. At `warn_at` the extension sends the lead one steering message
(converge, integrate, record outcomes) and records `lead_call_warning`; nothing is blocked. When
the count reaches the limit, every tool call except `orchestration_record` `status` and `finish`
is blocked with a `LEAD_CALL_LIMIT` reason that says so. The lead may finish within a fixed grace
of 2 further calls and its chosen outcome is recorded as given (a non-COMPLETED finish is
reconciled as usual, abandoning open work). If the lead has not finished by then, the extension
stops the run without finishing it: it records any worker still in flight as `blocked`
(`LEAD_CALL_LIMIT`), aborts worker handles and waits (bounded, 5 s) for them to settle (ClaudeWorker runs only: a Pi `spawn_subagent` has no abort handle here, so a subagent is not waited for), records `lead_call_limit_reached` (the recorder
writes a fresh resume checkpoint with reason `lead_call_limit` and `stopped_reason:
LEAD_CALL_LIMIT`, under the owner-checked write), releases the run lease through the owner
path, and aborts the agent. The run stays `IN_PROGRESS`, the session marker stays active and
later tool calls are refused. If a wave is `integrating` at the stop, the pre-integration checkpoint is kept and `mid_integration: true` recorded, so a resume cannot proceed. `resume` itself refuses with `RESUME_MID_INTEGRATION` whenever `mid_integration` is recorded or any current wave is still `integrating` (a conflicted or half-merged tree can sit under an unchanged HEAD, which the checkpoint cannot show); the lease is released again and the run stays `IN_PROGRESS`. **The way out:** `pico exec cancel` (which finishes the run and, through #42, abandons the open wave), inspect or clean the checkout, then start a new run. The headless execution ends `interrupted` with
`stop_reason: LEAD_CALL_LIMIT`. If the checkpoint cannot be written the run is not left
resumable; it stays closed and the normal settle ends it.

**Resume after a lead-call stop.** Only an explicit operator action continues it: `pico exec
start --resume --max-lead-calls N` (or `--resume-interrupted` with a higher task/manifest
limit, or a higher policy limit). The new limit must exceed the calls already made; the same,
a lower, or no effective limit is refused before anything launches with `LEAD_CALL_LIMIT_EXHAUSTED`.
The check uses the limit in the launcher's own execution record (`max_lead_calls`), not the run record's, because the run record's limit can be rewritten by anything holding the run's owner id; when the launcher pinned the limit in the environment, the recorder also refuses a later `lead_limit set` that differs (recorded in `refused_sets`). That environment pin is best-effort only, because the lead's bash inherits the environment and can override the variable; the authoritative resume check is the launcher's execution record (`headless_run.py`, `_launch_locked`), and an interactive run, which has no launcher record, has no exhausted check on resume. The usual resume rules apply (lease claim, checkpoint verification; drift refuses), the new
limit and source are recorded and the call count does not reset. Because the run is not terminal,
a repeat `start` without `--resume` reports `resume_required`. If the wall-clock limit also
fires (`EXECUTION_TIME_LIMIT`), the run is finished `BLOCKED` with that code as before and is
then terminal, so a later resume returns `already_finished`; the time limit never reopens, and
the lead-call stop never overrides an already terminal run. Garden and the scheduler show
`lead calls used/limit`, and `benchmark_metrics` reports `calls.lead_limit`.

**Repeated ineffective actions (`failure_retry_budget`).** The delegation policy's
`repeated_failure` escalates a failing route; this detector stops a step that repeats the same
failure without any change of state. It is deterministic and reads only structured fields.

*Signature.* `failure_signature_digest` is the SHA-256 of (task id, step id, route epoch, route,
failure code, checkout HEAD, inputs digest). The code is the worker result's `blocker_code`
(ClaudeWorker and Codex `spawn_subagent`), else `WORKER_FAILED`; an abort (`ABORTED`) is not a
failure. HEAD is the step's own bound worktree's; for an unbound step (a read-only reviewer) it is
the HEAD of the task's bound mutating worktree, where the task's real commits land; the main
checkout only when the task has none. It is read with `session_scope.py` `{"operation":"head"}`
after the worker returned, so a worker that committed and then failed is at its new HEAD. A HEAD
that cannot be read is diagnosed and sent as absent: the recorder treats it as no state change (it
matches any recorded HEAD, so it neither resets a count nor reopens a blocked step); with no
signature recorded yet the start is simply allowed. The inputs digest is
`PI_ORCHESTRATION_INPUTS_DIGEST`, which `workspace_run.launch` sets to the SHA-256 of
`dispatch_inputs` as they stand at launch: the repository pin and each upstream task's pin,
readiness, validated commit and contracts. It does **not** cover the status of approved input
files, and it is absent in a single-repo `pico exec` (and outside a workspace launch), where only
HEAD resets the count. A follow-up could digest the declared input paths. The extension sends `failure` events (written only by the extension:
the lead's `orchestration_record` schema does not accept them) and the recorder keeps
`parent_orchestration.failure_signatures`: `digest`, `kind`, the fields above, `count`,
`first_at`, `last_at`, `blocked`. The list holds at most 100 entries (the oldest unblocked one is
dropped first, so a blocked signature outlives it). The head is part of the signature, so the
entry's `head` is the last HEAD it was seen at.

*Gate.* Before `ClaudeWorker` or `spawn_subagent` starts a routed step, the extension sends
`retry_gate` with the effective budget. The recorder refuses when some `failure` signature of that
task, step and route epoch, at the current HEAD and inputs digest, has `count > retry_budget`: the
budget counts retries after the first failure, so the default of 2 lets three failures happen and
refuses the fourth start. A refusal records the step `blocked` with `REPEATED_INEFFECTIVE_ACTION`
(a `worker_overrides` entry, `by: extension`, when it had failed), marks the signature `blocked`,
and returns the signature to the extension, which blocks only that tool call with a reason naming
the step, failure code, count, budget and signature. It never sets `parentBlocked`, never changes
the phase and never touches other tasks; the lead may escalate the route, change the work and
retry, or finish PARTIAL/BLOCKED, and #42 reconciles whatever is open. A gate that cannot be
evaluated is diagnosed and lets the call through (it guards against waste, not against an unsafe
action). If the state then changed (another HEAD or digest) and the step still stands blocked by a
refusal, the next gate reopens it as `failed` with its earlier code (`reason: state_changed`) so
the start is a normal retry; the signatures stay as the record. A different route epoch (an
escalation) is a different signature outright. A retry that succeeds (#54's failed to completed
override) clears the step's signatures for that epoch. The gate runs as the lease owner like every
recorder write, so a non-owner cannot record failures or reopen a step.

*Prerequisites.* A blocker is a prerequisite when the worker result carries `blocker_kind:
prerequisite`, or its code is in the closed set `PREREQUISITE_CODES`
(`APPROVED_PILOT_INPUTS_UNAVAILABLE`, `PREREQUISITE_UNMET`, `MODEL_BACKEND_UNAVAILABLE`,
`MODEL_AUTH_UNAVAILABLE`). No naming pattern (such as a `_UNAVAILABLE` suffix) is trusted: the code
is worker-chosen, so a pattern would let any loop escape detection. Such a failure blocks the step
immediately with its own code and is recorded as a `prerequisite` signature (`blocked: true`); it
never counts toward `REPEATED_INEFFECTIVE_ACTION`, even with a budget of 0. A prerequisite is
recorded as blocked, not looped on: while the step stands blocked by it and neither HEAD nor the
inputs digest changed, the gate refuses another start with `PREREQUISITE_UNCHANGED` (distinct
from the loop code, never `parentBlocked`; the refused start is not a new failure). A changed HEAD
or digest reopens the step as `failed` with its prerequisite code (`reason: state_changed`) so the
start is a normal retry. If a step blocked by a prerequisite later exhausts a loop signature at a
changed state, the new refusal replaces the recorded code and is written to `worker_overrides`
(`by: extension`, `reason: retry_gate`, `from_blocker_code` the prerequisite), so the record shows
why the latest start was refused. The ways out of `PREREQUISITE_UNCHANGED` are a new commit in
the task's worktree (even an empty one), changed declared inputs, or an escalation to a new route
epoch. A change outside git (an approved file arriving untracked) is invisible to the digest.
Otherwise only that step stays blocked, and the lead can finish PARTIAL. With an unknown HEAD,
changed inputs still reopen the step. A new commit resets every signature count, as specified, so
a cheap commit can reset a loop; the lead-call limit (#47) remains the overall bound. Scientific validation can therefore report an unresolved approved
input as a blocked step without being stopped as a loop and without being restarted blindly.

*Step, not run.* The maintainer's refinement blocks the step, not the run, which replaces the
original issue's "repeating it after the block finishes the run `BLOCKED`" criterion: a refusal
must leave other tasks running, so it cannot end the run. Repeated refused starts of the same step
are bounded by the lead-call limit (#47) and the wall-clock limit rather than by a run outcome;
the lead is told at every refusal and can finish PARTIAL or BLOCKED itself.

*Configuration.* `failure_retry_budget`, an integer from 0 to 1000 (`FAILURE_RETRY_BUDGET_INVALID`
otherwise; 0 means no retry): `pico exec start --failure-retry-budget N` (source `cli`) or the
workspace task's value (`task`), then the manifest's (`manifest`), then `limits.failure_retry_budget`
in the target's `.pi/autonomy.yaml` (`policy`), then 2 (`default`). The launcher records it in the
execution record (`failure_retry_budget`, `failure_retry_budget_source`, `inputs_digest`) and hands
it to the child as `PI_ORCHESTRATION_FAILURE_RETRY_BUDGET`, `..._SOURCE` and
`PI_ORCHESTRATION_INPUTS_DIGEST`; the extension reads the policy itself when none is given and an
invalid value blocks the run closed. The run record keeps the applied value in
`parent_orchestration.failure_retry_budget`. Like the lead-call pin, the environment hand-off is
not a security boundary (the lead's shell inherits it). Not covered yet: failures of `pico workspace
validate` checks are not recorded as signatures.

**Checkout hygiene.** Before launching, the runner records the target's
branch (or detached `HEAD`) and its linked worktrees. After the child exits it
removes every worktree the run created that is clean (`git worktree remove`
without `--force`; a worktree with uncommitted or untracked files is kept and
reported), prunes stale entries, and, if the parent checkout moved and has no
uncommitted tracked changes, switches it back to where it started. Branches
are never deleted, since they hold the work. The result is recorded as the
execution's `checkout` block (`restored`, `removed_worktrees`,
`kept_worktrees`, `left_reason`, and `dirty_files`/`dirty_count`: the tracked
changes the run left uncommitted in the parent checkout itself, `null` when
Git cannot answer; `new_untracked`/`new_untracked_count`: untracked, not
ignored entries that appeared in the parent checkout during the run, an
untracked directory counting as one entry and `.pi/worktrees/` excluded) and
summarised on the workspace task entry. Untracked entries are a report only:
they never refuse a launch, because they cannot change a commit or a check at
the recorded commits, but they show a write outside the run's worktrees (#63).
Nothing dirty is ever cleaned, stashed or reset.
This keeps the next task in the same repository launchable: the launcher
requires the checkout to be at its pinned commit. The orchestrate prompt also
tells the lead to integrate in a dedicated integration worktree and never
switch the parent checkout; this restore is the safety net when it does not.

**Headless is not autonomous.** If the target's `.pi/autonomy.yaml` grants
`create_worktrees`, `commit`, `push_branch`, `create_pr`, `mark_pr_ready`,
`merge` or `deploy`, `start` refuses with `MUTATION_NOT_AUTHORIZED` unless
`--allow-mutation` is passed; the flag is recorded on the execution. The
policy itself is never modified, so what a mutating run may do remains the
policy's decision. An objective that looks like it contains a secret is
refused (`OBJECTIVE_INVALID`).

**Exit status.** 0 when the execution is running or ended `COMPLETED` /
`PARTIAL`; 1 when it is `BLOCKED`, `FAILED`, interrupted, cancelled or refused
(the JSON carries `error_code`). Coverage: `tests/test_headless_run.py` drives
a fake `pi` on PATH that creates and finishes, settles, crashes or hangs a
real parent run through `tools/parent_orchestration.py`.

### Repository-scoped child launcher

`tools/workspace_run.py` (launcher: `pico workspace`) turns one task of a
workspace manifest into one headless execution (`tools/headless_run.py`)
bound to exactly one repository entry. The coordinator thereby authorizes
which child orchestrators exist; every child remains an ordinary parent
orchestration run with a single-valued `target_repository`, its own worktrees,
state and GitHub identity, so the existing scope checks still stop a worker
from acting on another repository.

```sh
python3 tools/workspace_run.py launch workspace.yaml --task <id> [--allow-mutation] [--resume] [--no-wait] [--no-access-check]
python3 tools/workspace_run.py status workspace.yaml [--task <id>] [--no-access-check]
python3 tools/workspace_run.py cancel workspace.yaml --task <id> [--no-access-check]
```

**Preconditions, all fail closed before anything is launched.** The read-only
plan is recomputed: a repository blocker for the task's repository
(`TARGET_REPOSITORY_*`, `WORKSPACE_REPOSITORY_DUPLICATE`,
`WORKSPACE_PIN_MISSING`, access) or a task blocker is returned as is; a task
whose `blocked_by` is not empty is refused with `WORKSPACE_TASK_BLOCKED`; a
checkout whose `HEAD` is not the pinned commit is refused with
`WORKSPACE_PIN_MISMATCH` (the launcher never checks anything out); and, when
a child would really start or resume, a checkout with uncommitted tracked
changes (staged or not; untracked files do not count) is refused with
`WORKSPACE_CHECKOUT_DIRTY`, naming the files and any task whose run left them
(#68). The cleanliness check also applies to scheduler dispatches, which skip
only the separately gated budget check. The child's
objective names the workspace, task, repository, pinned commit and the
upstream tasks it may consume, and instructs the run to stay inside its
repository.

**Identity and linking.** The execution id is `<workspace-id>--<task-id>`,
stored under the repository's own execution state, so the same id from another
checkout is simply unknown there. The workspace record at
`<state>/workspaces/<workspace-id>.json` holds, per task: repository, resolved
GitHub identity, pinned commit, execution id and status, launches, child run
id, run status, blocker code and trace path. It never contains the objective,
prompts, transcripts or secrets. `status` reconciles each task against the
live process and provenance; `cancel` delegates to the headless cancel.
Relaunching a finished task returns `already_finished`; a crashed child is
`interrupted` and `--resume` continues the same parent run.

Coverage: `tests/test_workspace_run.py` launches three children in three
temporary repositories with the fake `pi` and asserts every checkout is
byte-identical afterwards, run ids are distinct and linked, executions are
bound to one repository, relaunch is idempotent, crash resumes the same run,
blocked tasks and pin mismatches are refused, and cancel closes the run.

### Cross-repository scheduler

`tools/workspace_scheduler.py` (launcher: `pico workspace schedule`) dispatches
the dispatchable tasks of a workspace manifest through the child launcher. It
is a local process over the persistent execution and workspace records; there
is no broker, queue or service.

```sh
python3 tools/workspace_scheduler.py workspace.yaml [--max-parallel N] [--allow-mutation] [--resume-interrupted] [--json] [--no-access-check]
```

**Concurrency.** The cap is the manifest's `max_parallel`, lowered (never
raised) by `--max-parallel`. At most one child per repository is active at a
time, so two parent runs never share a checkout. Candidates are taken in
`(wave, task id)` order. Children run in threads that wait on their own
headless process, so the scheduler reaps them itself.

**Readiness frontier.** A task is dispatched only if the plan marks it
`dispatchable`: declared `planned`, nothing in `blocked_by`, not on a cycle.
A child ending `COMPLETED` or `PARTIAL` does not change declared readiness,
so dependents stay `waiting_for_readiness` until the manifest declares the
upstream task `ready_for_downstream` or `merged`. One invocation therefore
dispatches one frontier. Each dispatch records its immutable inputs in the
workspace record: the repository identity and pinned commit, every upstream
task's repository, pinned commit and declared readiness, and the orchestrator
revision. The child's objective names the same upstream pins and forbids
consuming unverified branch contents.

**Task states.** `completed`, `partial`, `failed` (child ended `BLOCKED` or
`FAILED`), `cancelled`, `refused` (dispatch precondition failed, for example
`WORKSPACE_PIN_MISMATCH` or `WORKSPACE_CHECKOUT_DIRTY`), `needs_reconciliation` (child interrupted or launch
outcome uncertain), `blocked_by_failure` (a transitive dependency is in one of
the four failure states), `waiting_for_readiness`, `declared_<readiness>`
(not scheduled because its readiness is not `planned`), and
`not_dispatchable` (cycle or task blocker). Failure blocks only dependents;
independent tasks run.

**Restart and reconciliation.** Every launch goes through the idempotent
execution records, so a restart never starts a second child, branch or PR for
a recorded task. Children run in their own sessions and outlive the
scheduler; a restart observes a still-running child and waits for it instead
of relaunching it. A headless launch persists a `launching` state before the
process exists, so a crash between the two is seen as uncertain, never as
never launched. Interrupted children are not relaunched by default: the run
reports `needs_reconciliation` and exits 1, and `--resume-interrupted`
continues each one in its existing parent run. A refusal is not persisted as
a launch, so the next run retries it after the cause is fixed. A per-workspace
lock refuses a second concurrent scheduler (`WORKSPACE_SCHEDULER_ACTIVE`).
The last 20 scheduler sessions are kept in the workspace record with their
cap, dispatched, resumed, observed and refused tasks.

**Headless resume fix.** A resumed headless run must not send the
`PI_ORCHESTRATION_RUN_V1` marker, because the lifecycle answers it by settling
the restored run and starting a new one. `tools/headless_run.py` now resumes
with a continuation prompt when the recorded run is still `IN_PROGRESS`, and
with the full orchestration prompt when no run was ever created. Cancel now
finishes the parent run with `EXECUTION_CANCELLED` before signalling the
process, so a graceful Pi shutdown cannot settle it first with another code.

**Exit status.** 0 when nothing is in a failure state or blocked by one; 1
otherwise, or when another scheduler holds the workspace; 2 for an invalid
manifest. Coverage: `tests/test_workspace_scheduler.py` measures peak
concurrency from the fake `pi`'s start and end log across four temporary
repositories, and covers per-repository serialization, failure isolation,
the readiness frontier with recorded upstream pins, restart idempotency,
observing a child left running by a dead scheduler, explicit reconciliation of
an interrupted child, retry of a refused dispatch, and the scheduler lock.

### Cross-repository contracts and coordinated draft PRs

`tools/workspace_contracts.py` (launcher: `pico workspace validate|publish|ci|promote`)
turns readiness into observed evidence. The manifest stays the plan; observed
state lives in each task's `observed` block in the workspace record, and the
planner overlays it (`workspace_plan.plan(..., observed=...)`): effective
readiness is the further along of declared and observed.

**Manifest additions** (`schema_version` stays 1, all optional): per
repository `base_branch` (default `main`); per task `kind`
(`implementation` | `qualification`), `issue`, `provides` and `requires`
(`{contract, version}`), and `checks` (`{id, repository, argv,
timeout_seconds}`). Static blockers: `WORKSPACE_CONTRACT_UNSATISFIED` (a
required contract is not provided by a direct dependency),
`WORKSPACE_CONTRACT_VERSION_MISMATCH`, `WORKSPACE_CONTRACT_DUPLICATE`,
`WORKSPACE_CHECK_REPOSITORY_UNKNOWN`, `WORKSPACE_CHECK_SCOPE_INVALID` (an
implementation task's checks must run in its own repository), and
`WORKSPACE_QUALIFICATION_INVALID` (a qualification task needs checks and a
dependency).

**Readiness evidence.**

| State | Set by | Evidence |
| --- | --- | --- |
| `implemented` | launcher | child run ended `COMPLETED` or `PARTIAL` |
| `validated` | `validate` | every check passed on one commit; provided contracts recorded at their versions |
| `ready_for_downstream` | `promote` | draft PR exists, PR head is the validated commit, CI observed green now |
| `merged` | `ci` (observed) | PR state merged at the validated commit |

**Gating.** An implementation task is dispatchable only when every dependency
is `ready_for_downstream` or `merged` and every contract it requires from that
dependency is observed validated at the required version; a dependency the
manifest declares `ready_for_downstream` by hand still blocks until its
contracts are validated (`merged` is the only declared state accepted without
observed validation). A qualification task needs its dependencies `validated`
or further, and launches no child run (`WORKSPACE_TASK_NOT_LAUNCHABLE`): the
scheduler runs its checks as a qualification wave, each repository at its
dependency's validated commit, or at its pin if no dependency lives there
(`WORKSPACE_QUALIFICATION_AMBIGUOUS` if two dependencies in one repository were
validated at different commits). Dispatch inputs record each upstream task's
validated commit, contract versions and PR, and the child objective names
them.

**`validate`.** An implementation task must be `implemented`
(`WORKSPACE_TASK_NOT_IMPLEMENTED`) and `--head` must be a local `pico/` branch
descending from the pinned commit (`WORKSPACE_HEAD_INVALID`,
`WORKSPACE_HEAD_UNRELATED`). Every check runs with `shell=False` in a
temporary `git worktree add --detach` under `<state>/checks/`, removed
afterwards; only status, exit code, duration and a log path are recorded. A
failed check clears any earlier validation (`WORKSPACE_CHECK_FAILED`).
Released tasks cannot be revalidated (`WORKSPACE_TASK_ALREADY_RELEASED`).

**`publish`.** Requires `validated`, an `issue`, and the head branch still at
the validated commit (`WORKSPACE_HEAD_CHANGED`). Every upstream implementation
task must already have a PR, unless it is `merged`
(`WORKSPACE_UPSTREAM_UNPUBLISHED`). The body (`Refs #<issue>`, or
`Closes #<issue>` when the task sets `closes_issue: true` or omits it in a
repository whose `.pi/autonomy.yaml` on GitHub's default branch sets
`issue_reference: closes`; validated
commit, check results, contracts, `Depends on: owner/repo#N` lines, no
objective or transcript, secret-scanned) goes to
`github_integration.publish` unchanged: autonomy `push_branch` and
`create_pr` are required on the default branch's policy on GitHub
(`PUBLICATION_NOT_AUTHORIZED`), the run is a dry run
until `--apply` with the reviewed `--plan-hash`, the push is non-force, and the
PR is a draft. The PR number and publication run id are recorded.

**Republishing after a closed PR (#67).** A branch that already has a closed
or merged PR is refused (`PUBLICATION_PR_CONFLICT`): a closed PR's branch is
never reused, reopened or overwritten. When every PR of the task's branch was
closed without merging, `publish --new-branch` pushes the validated commit to
the first `<branch>-<n>` (n from 2) that holds this task's earlier
republication (the open PR recorded in the task's `pr`, whose remote head is
the validated commit or one of its ancestors, or a branch already at the
validated commit with no PR, left by an apply that pushed but did not create
the PR; it is reused), or is free: no PR, not on the remote, no local branch,
and not the head branch or PR branch of another task of the same repository.
A candidate with any PR the task's record does not name is skipped, never
taken over and never listed as superseded, except an open one already at the
validated commit: that is most likely this task's own PR from a creation whose
answer was lost, so it refuses (`PUBLICATION_PR_CONFLICT`, naming the PR)
instead of opening a second PR for the same commit. No command adopts such a
PR: close it, then publish again, and the commit goes to the next free
branch. Branches reserved by another task are skipped before this check. This task's own closed PRs met on the way are superseded as
well, and a merged one refuses. Pass `--new-branch` to the apply as well: the
plan hash binds `remote_head`, so an apply without it is refused. The body adds
`Supersedes #N (closed without merging).`, the plan records `remote_head` and
`supersedes_prs` (bound by the plan hash; a branch that changed between the
dry run and the apply gives `GITHUB_STATE_CONFLICT`), and the task's `pr`
records `branch` and `supersedes`. The observed CI and `promoted_at` of the
superseded PR are dropped, and a `ready_for_downstream` task goes back to
`validated`, so downstream waits until `promote` observes green CI on the new
PR. An apply that changes nothing (same PR, no mutation) leaves the task's
record, its publication run and its promotion as they were. A GitHub or git failure while choosing the branch returns
`PUBLICATION_FAILED` instead of a traceback. The local branch keeps its name. `--new-branch` on a branch with
no PR or an open one is refused with `PUBLICATION_NEW_BRANCH_UNNEEDED`.

**`ci` and `promote`.** `ci` reads `gh pr view --json
statusCheckRollup,headRefOid,state` (allowlisted, read-only):
`github_sync.classify_checks` yields `green` only when at least one check
exists and all passed, otherwise `red`, `pending` or `none`; a PR head other
than the validated commit is `stale`. `promote` requires `green` right now
(`WORKSPACE_CI_NOT_GREEN`) and records `ready_for_downstream`. Nothing here
merges, marks a PR ready, or pushes to a base branch.

Coverage: `tests/test_workspace_contracts.py` runs the whole lifecycle on two
temporary repositories with the fake `pi`, a fake GitHub adapter, and `git
ls-remote`/`git push` redirected to local bare repositories, so the real
non-force publication path runs offline.

### Workspace Garden view

`tools/watch.py --workspace <id|manifest>` (launcher: `pico watch --workspace
...`) serves a read-only, loopback-only view of one workspace. It still binds
only to `127.0.0.1` and handles only `GET`.

```sh
python3 tools/watch.py --workspace workspace.yaml --state-dir <state> [--port 8765]
python3 tools/watch.py --workspace <workspace-id> --state-dir <state>
```

**Sources.** With a manifest path, the task graph (waves, `depends_on`,
`blocks`, declared readiness, `blocked_by`) comes from
`workspace_plan.plan(path, access_check=None)`. That call runs only the
planner's read-only Git probes, and its result is cached until the manifest
changes. With an id, the graph comes from the workspace record alone:
recorded tasks, dependencies taken from each dispatch's `inputs.upstream`,
and no waves or readiness. Both modes read
`<state>/workspaces/<id>.json` and the child run records
`<state>/runs/<run_id>.json`. Execution records under `<state>/executions/`
contain the child objective, so they are never opened. A child run is
attributed to a task only when its `target_repository` matches the task's
recorded identity.

**States.** Task states come from `workspace_scheduler.report(plan, entries,
refused)`, so they are exactly the scheduler's: `completed`, `partial`,
`failed`, `cancelled`, `refused`, `needs_reconciliation`,
`blocked_by_failure`, `waiting_for_readiness`, `declared_<readiness>`,
`not_dispatchable`, `running`, `not_launched`. Refusals are not persisted as
launches. The view therefore applies the most recent scheduler session's
refusals to tasks that have not been launched since, which reproduces that
session's final report. Colours: running is teal; completed and declared
`merged` / `ready_for_downstream` are green; partial, needs reconciliation,
blocked by failure and not dispatchable are amber; failed, cancelled and
refused are coral; everything else is gray. The workspace record changes
when a launch returns, when `pico workspace status` reconciles, or when a
scheduler session starts or ends. A child launched by a scheduler that is
still waiting on it therefore keeps its recorded state until then. The
child's own run status, read live from its run record, is shown next to it.

**Endpoint.** `GET /api/workspace` returns `{ok, workspace_id, source
("manifest"|"record"), record_found, updated_at, plan_status, max_parallel,
waves, status ("OK"|"ATTENTION"), counts, repositories[], tokens,
sessions[]}`. Each repository has `{id, repository, pinned_commit,
blocker_code, tasks[], tokens}`. Each task has `{id, repository, wave
(0-based), readiness, state, depends_on, blocks, blocked_by,
execution_status, launches, run_id, run_status, run_blocker_code,
refusal_code, run {id, status, blocker_code, started_at, ended_at,
phases_total, phases_completed}, links[], tokens}`. `links` are the PR URLs
`publication_links` verifies for the child run. Each session has `{id,
started_at, finished_at, cap, dispatched, resumed, observed_running, refused
{task: code}, status}`. Errors are `{ok: false, error_code, error}`:
`WORKSPACE_UNKNOWN` (404, id mode with no record), `WORKSPACE_ID_INVALID`
(400), `WORKSPACE_MANIFEST_INVALID` or `WORKSPACE_RECORD_UNREADABLE` (422),
`WORKSPACE_MODE_OFF` (404, viewer not started with `--workspace`). In
workspace mode `GET /api/runs` returns exactly the workspace's child runs,
so `#run=<run_id>` opens a child in the existing run view.

**Tokens.** Per child run, `input_tokens`, `output_tokens` and
`cached_tokens` add `parent_orchestration.context.lead_usage` and every
`worker_usage` backend. `total_tokens` is set only when the lead and every
backend reported complete totals, the same rule as
`parent_orchestration.delegation_metrics`. Repository and workspace blocks
sum their runs and add `runs` and `total_complete`. The page shows input plus
output, and the reported total separately.

**Privacy.** Every served value is whitelisted and shape-checked: ids,
GitHub identities, commit SHAs, state names, codes, timestamps and
counters. Task titles, the workspace goal, objectives, prompts, transcripts,
raw outputs, trace paths, local checkout paths, planner error details and
logs are never served. Coverage: `tests/test_watch_workspace.py` builds a
workspace record and run records in a temporary state directory. It checks
grouping, that states match `workspace_scheduler.report`, token sums, PR
links, record-only mode and error codes. A sentinel string in the manifest,
the record, the run records, a fake execution record and a fake session
transcript never appears in either HTTP response.

### Garden controls

`tools/garden_controls.py`, enabled by `pico watch --workspace <manifest> --controls`,
lets the Garden trigger `workspace_contracts` actions. It never reimplements
their gates. Without the flag no POST endpoint exists (`CONTROLS_OFF`).

**Security.** Every request, reads included, must carry `Host:
127.0.0.1:<port>` or `localhost:<port>`; anything else is answered `421
HOST_REJECTED` (DNS-rebinding guard). `POST /api/action` additionally requires
`Origin` to be the Garden's own origin (`CONTROL_ORIGIN_REJECTED`), the
per-session token in `X-Pico-Token` (`CONTROL_TOKEN_INVALID`; generated with
`secrets.token_urlsafe(32)`, printed once in the launch URL's fragment, which
the page reads and removes from the address bar), a JSON body under 16 KiB
(`CONTROL_BODY_INVALID`), a known action (`CONTROL_ACTION_UNKNOWN`) and a valid
task id (`CONTROL_TASK_INVALID`). Actions run one at a time
(`CONTROL_BUSY`). The existing CSP (`default-src 'self'`, `form-action
'none'`) is unchanged.

**Preview and confirm.** A call without `confirm` returns `stage: preview` and
an HMAC-SHA256, keyed per session, over the exact state previewed:

| Action | Preview | Bound to |
| --- | --- | --- |
| `validate` | repository, branch, commit, check argv | branch name, branch commit, the task's checks |
| `publish` | `github_integration.publish` dry-run plan and full PR body | the plan hash |
| `promote` | live CI status, check count, PR state and head | CI head, status and PR state |
| `ci` | none (read-only on GitHub) | n/a |

The confirming call recomputes that state. A mismatch is refused with
`CONTROL_PREVIEW_STALE`, for example after a new commit on the branch or a CI
change. Precondition failures come back as `stage: refused` with the
underlying code, such as `PUBLICATION_NOT_AUTHORIZED` or
`WORKSPACE_TASK_NOT_VALIDATED`. Every non-preview outcome is appended to
`garden_actions` in the workspace record (last 100: time, action, task, stage,
ok, error code). Publication also creates its own provenance run, as on the
command line.

**Read-only endpoints.** `GET /api/controls` returns, per repository, the
autonomy policy source, profile, PR granularity, the ten boolean permissions,
`merge.enabled` and the integer limits, plus the effective models and
ambiguity capacities per family and tier from `model_profiles`. It never
returns the workspace goal or task titles. `GET /api/branches?task=<id>`
(controls only) lists the task repository's local `pico/` branches with their
commit and whether they descend from the pin. With `--controls` on, every
data endpoint (`/api/controls`, `/api/branches`, `/api/runs`,
`/api/workspace` and `/api/issues`) requires the session token in
`X-Pico-Token` (compared in constant time, as for actions) and answers `403
CONTROL_TOKEN_INVALID` without it. The page sends the token on every poll; a
page opened without it shows "Open the URL printed at launch". The static
page, script and stylesheet are served without the token, since the token
reaches the page only in the URL fragment. The page keeps the token in memory
only and removes it from the address bar, so a reload or a second tab opened
from the address bar has no token and shows only that message. Open the full
launch URL printed in the terminal again to recover. The token is deliberately
not kept in `sessionStorage` or any other browser storage. Without `--controls` nothing needs
a token, and `/api/controls` stays a read-only panel. A
malformed or negative `Content-Length` on `POST /api/action` is answered with
`400 CONTROL_BODY_INVALID`.

Coverage: `tests/test_garden_controls.py` starts a real server on two
temporary repositories with a fake GitHub adapter. It covers every gate, the
off mode, the panels, a tampered confirmation, a branch moved after its
preview, CI changing between preview and confirmation, the full validate,
request PR, CI and promote flow with its audit log, and the busy lock.

**Workspace-level and editing actions.** All follow the same preview and
HMAC confirmation and are audited:

| Action | Preview | Bound to | Effect |
| --- | --- | --- | --- |
| `cancel` | execution status and run | execution id, run id, status | `workspace_run.cancel` |
| `schedule` | tasks to launch and resume, cap, mutating permissions per repository | those lists, `allow_mutation`, cap | `workspace_scheduler.run` in a background thread; one at a time |
| `set_permission` | unified diff of `.pi/autonomy.yaml` (created from the template if absent) | repository, permission, value, SHA-256 of the file before | atomic write (unique temp file, fsync, replace), schema-validated, comments kept; not committed. Refused with `AUTONOMY_POLICY_PATH_UNSAFE` when `.pi` or `.pi/autonomy.yaml` is a symlink or resolves outside the checkout |
| `set_model` / `reset_model` | current and new model and capacity | family, tier, target, SHA-256 of the user model config | `model_profiles.cmd_set` / `cmd_reset` |

**No one-shot publication grant.** The Garden `publish` action used to take
`grant_once` to publish once when the default-branch policy withheld
`push_branch` and `create_pr`. It was removed (#59): a request that still
carries `grant_once` is refused with `CONTROL_BODY_INVALID`, and a refusal
tells the user to allow the permissions in the policy, commit it and merge it
into the default branch.

**Editable scope.** `set_permission` accepts only `create_backlog_items`,
`create_github_issues`, `create_worktrees`, `commit`, `push_branch`,
`create_pr`, `comment_on_issue` and `comment_on_pr`; `mark_pr_ready`, `merge`
and `deploy` return `CONTROL_PERMISSION_LOCKED`. A permission that is not a
single plain `name: true|false` line returns `AUTONOMY_POLICY_UNEDITABLE`.
`set_model` offers only models already bound in that family (current tiers or
the base policy), so no unknown Claude backend mapping can be introduced;
`cmd_set` still enforces the tier's ambiguity floor.

### Garden issues panel

`tools/garden_issues.py` serves `GET /api/issues`, a read-only list of the
open issues waiting in the watched repository. The panel sits under the run
and workspace views (`viewer/issues.js`). It never changes GitHub or Git, and
it has no queue action yet; the issue queue (#34) is a separate slice.

**Targets.** With `pico watch .` the target is the checkout the viewer was
started on. With `pico watch --workspace <manifest>` it is every repository
in the manifest, one block per repository in manifest order, because a
workspace mission draws its work from all of them. A workspace id without a
manifest, and `pico watch --all`, have no checkout to resolve, so the
endpoint answers `404 ISSUES_TARGET_NONE` and the panel stays hidden. On
every request each repository's identity is re-resolved with
`github_integration.resolve_target` (the `--repo` value, or the manifest's
declared `repository`, is the explicit target). `TARGET_REPOSITORY_AMBIGUOUS`,
`TARGET_REPOSITORY_UNRESOLVED` and `TARGET_REPOSITORY_CONFLICT` (the remotes
no longer match the watched identity) fail closed: no `gh` call is made and
the repository shows the code. A manifest repository with a planner blocker
shows that blocker code instead.

**Data and cache.** `gh` is reached only through two fixed-argv read
wrappers in `tools/github_sync.py`: `list_issue_summaries` (`gh issue list
--state open --json number,title,state,labels,assignees,updatedAt,url --limit
51 --repo <identity>`) and `list_open_prs` (`gh pr list --state open --json
number,headRefName,body,isDraft --limit 101 --repo <identity>`). At most
`CAP = 50` issues are served, in `gh`'s order (newest first), and `truncated`
says when more exist. Each `gh` call has a 10-second timeout (`GH_TIMEOUT`;
the CLI default is 30). Each repository's result, including a failure, is
cached for 60 seconds, and each request drops expired entries and
repositories that are no longer watched. The cache lock is never held while
`git` or `gh` runs. A repository already being fetched is not fetched again:
a concurrent request gets its expired entry as `cached: true`, or
`ISSUES_FETCH_IN_PROGRESS` when none exists yet. Workspace repositories are
read in parallel (up to 4 at a time), so one hung repository does not delay
the others. The page polls every 30 seconds without blocking the live run
view.

**Response.** `{ok: true, mode ("repository"|"workspace"), cap,
cache_ttl_seconds, reference_rules, repositories[]}`. Each repository is
`{id (manifest id or null), repository, available, error_code, error,
fetched_at, cached, truncated, issues, pull_requests {available, error_code,
scanned, truncated}, branches {available, scanned}}`. `issues` is `null`
whenever `available` is false, so an unavailable list is never shown as an
empty one. Each issue is `{number, title, labels [names], assignees [logins],
updated_at, url, references {branches, pull_requests}}`. The URL is rebuilt
from the verified identity and the number, not taken from `gh`.

**References**, each `null` when it could not be determined:
`branches` are local `refs/heads/pico/*` branches whose name has an
`issue-<n>`, `issues-<n>` or `gh-<n>` token (for example `pico/issue-33`;
`pico/batch-1` does not refer to issue 1). `pull_requests` are open PRs, the
first 100, whose body uses a GitHub closing keyword for the issue (`Closes
#33`, or `fixes owner/repo#33` for the same repository) or whose head branch
has the same token. `gh` 2.45 has no `closingIssuesReferences` field, so PRs
linked only in the GitHub UI, and mentions such as `Refs #33`, are not seen.

**Errors.** Per repository: `GH_UNAVAILABLE` (no `gh` on PATH),
`GH_UNAUTHENTICATED`, `GH_RATE_LIMITED`, `GH_TIMEOUT`, `GH_OUTPUT_INVALID`,
`GH_COMMAND_FAILED`, the identity codes above,
`TARGET_REPOSITORY_UNREADABLE` (`git` is missing or timed out while resolving
the identity), `ISSUES_FETCH_IN_PROGRESS`, or a manifest blocker code.
`error` is a fixed sentence per code; `gh`'s own output is never served. A
failed PR listing leaves the issues available and sets
`pull_requests.error_code`, with every issue's `references.pull_requests`
`null`. Endpoint level: `404 ISSUES_TARGET_NONE`, `422
WORKSPACE_MANIFEST_INVALID`, `403 CONTROL_TOKEN_INVALID` (controls on, no
token) and `421 HOST_REJECTED`.

**Rendering.** Issue titles, labels and logins come from GitHub users. The
API serves them verbatim as JSON strings, and `viewer/issues.js` writes them
only with `textContent`; links are made only for `https://github.com/` URLs.
Each row carries `data-repository` and `data-issue`, and
`PicoIssues.render(container, data, {rowAction})` accepts a callback that
returns a control for each row. The queue slice can use it for "add to
queue".

Coverage: `tests/test_garden_issues.py` (fake `gh` adapter, real temporary
Git checkouts and a real HTTP server) covers the shape, the cap, every `gh`
failure code, the cache and its pruning, a `git` timeout or missing `git`
on one repository, a hung `gh` that blocks neither other repositories nor
other requests, ambiguous and conflicting identities, workspace blockers,
references, the token on `/api/issues`, `/api/runs` and
`/api/workspace` with controls on and off, and that hostile issue text is
served verbatim. `tests/garden-issues.test.ts` renders hostile titles and
labels with a DOM stub that throws on `innerHTML`.

### Workspace time limit

`pico workspace launch` and the scheduler launch each child with the task's
`max_duration_seconds`, else the manifest's, else none. The task entry in the
workspace record carries `max_duration_seconds`, `deadline_at` and
`deadline_epoch` for that launch, and, after a stop, `stop_reason:
EXECUTION_TIME_LIMIT` and `time_limit`; `run_blocker_code` is
`EXECUTION_TIME_LIMIT`. A scheduler-launched child waits in its worker thread,
which enforces the deadline; children it only observes are checked on each poll
through `workspace_run.status`. A stopped task is a `cancelled` task, so it
blocks its dependents like any other failure and the run exits 1. The
scheduler text shows `(limit 600s, 411s left)` for a running child and
`(stopped after 601.2s of a 600s limit)` for a stopped one; `--json` rows carry
`time_limit`. The Garden task card shows the same as text (it only reads the
workspace record and never stops anything, so the remaining time is as fresh as
the last `status` or scheduler poll). Relaunching a stopped task does nothing
(`already_finished`, the stop reason is kept); resume the work with a new task
or execution id. Unlike the budget, which gates launches, this limit stops a
running child.

### Workspace budget

An optional manifest `budget` (`max_tokens` and/or `max_cost_usd`) limits
spend across the workspace's child runs. `tools/usage.py` (shared by the
Garden and the scheduler) sums each linked child run's recorded
`parent_orchestration.context`: input plus output tokens of the lead and every
worker backend, and the backend-reported `estimated_cost_usd`, which only
Claude workers report today (so the cost limit is a lower bound). The
scheduler recomputes spend before every launch; once either limit is reached
it launches nothing new and reports those tasks as `held_by_budget` (amber; the
session records them and the run exits 1). Running children are not stopped
and qualification checks still run. Raising the budget in the manifest lets
the next scheduler run dispatch them. The Garden shows spend against the
budget, and the schedule preview refuses to launch while it is exhausted.

A direct `pico workspace launch` (also with `--resume`) applies the same gate:
while the budget is exhausted it is refused with `WORKSPACE_BUDGET_EXHAUSTED`,
whose message gives spent against limit. `--ignore-budget` (`ignore_budget=True`
in `workspace_run.launch`) overrides it explicitly; the override is never
silent, because the task entry in the workspace record gains
`budget_override` (UTC time, exhausted limits and the spend at launch). The
check runs only when a child would actually start or resume: a repeat launch
that is a no-op (such as `already_finished`) is never refused and records no
override. It runs outside the workspace lock, so two simultaneous direct
launches can both pass it; this is accepted because spend is only known after
runs finish. The scheduler has already gated its own dispatch and passes the
internal `budget_checked=True`, which skips the check and records nothing
(`ignore_budget` is reserved for explicit user overrides).
The spend computation is `workspace_run.budget_state`
(`workspace_scheduler.budget_state` remains as an alias).

**Committing a policy change.** The Garden's `commit_policy` action commits
only `.pi/autonomy.yaml` (`git commit --only`), with the repository's own Git
identity, and only when `HEAD` is the repository's `base_branch`
(`POLICY_COMMIT_BRANCH` otherwise, so a policy change never lands on a `pico/`
work branch). The policy must validate and have an uncommitted change
(`POLICY_UNCHANGED`). The preview shows the diff and is bound to `HEAD` and the
diff. Nothing is pushed.

### Synthetic pilot: deterministic regression suite and real-agent benchmark

The synthetic workspace of `benchmarks/synthetic-workspace/` (two repositories, four tasks, a
provides/requires contract, a budget and time limits) is the main regression benchmark for
orchestration. It has two parts that never mix, so that model variance and cache effects cannot hide a
change in scheduler correctness, and CI never spends tokens.

**Deterministic suite (CI, every pull request).** `tests/test_synthetic_pilot.py` runs the workspace
through the real `workspace_scheduler`, `workspace_run` launcher, run lease, recorder
(`tools/parent_orchestration.py` through its CLI, with the lease owner), `workspace_contracts`
(`validate`, `publish`, `ci`, `promote` against a fake GitHub and bare repositories on disk) and
`benchmark_metrics`. Only the model is replaced: the suite's fake `pi` takes the lease and starts or
restores the run, and a scripted lead (`tests/scenario_lead.py`, scenario files are JSON lists of ops)
records events, applies the `reference/` solution on a `pico/<task>` branch, finishes, hangs, crashes or
makes N lead calls. A refused recorder call or an unscripted task fails the test. The scenarios are
`happy-multi-repo` (concurrent wave over two repositories, publish, CI green, promote, qualification),
`dependency-gate`, `partial-and-reconcile` (#42, #48 `PREREQUISITE_UNCHANGED`), `budget` (in-flight
spend, hold, direct-launch gate, `--ignore-budget`), `interrupt-resume` (SIGKILL, stale lease,
takeover, checkpoint verification, and checkpoint drift refused), `time-limit` (#46, injected clock),
`lead-call-limit` (#47, warning, resumable stop, same limit refused, raised limit continues) and
`retry-gate` (#48 `REPEATED_INEFFECTIVE_ACTION`). Each compares the provenance it left behind with a
golden record, `tests/fixtures/golden/<scenario>.json`: the facts of `benchmark_metrics.provenance_facts`
(no timestamps or ids) compared both ways with `diff_facts`, task states, lead calls and tokens.
Regenerate them deliberately with `PICO_UPDATE_GOLDEN=1 python -m pytest -q tests/test_synthetic_pilot.py`
(refused when `CI` is set; review the diff). The suite takes about 40 seconds, uses temporary
directories and bounded polling rather than fixed sleeps, sets a local Git identity in every repository it
commits to, and waits for whole process groups to exit after a kill. It does not prove that a model
records these events, the TypeScript extension's enforcement (`tests/orchestration-lifecycle.test.ts`),
real GitHub behavior, or that integrated waves are merged. CI runs it as its own step; the full run skips
those files so nothing runs twice.

**Real-agent benchmark (started by hand, never in CI).**
`benchmarks/synthetic-workspace/run-real.sh <40-hex revision> <label>` (`run_real.py`) is the single
entry point. It exits 2 and creates nothing unless `PICO_REAL_BENCHMARK=1` is set. It runs setup,
`pico workspace schedule` with `PI_CODING_ORCHESTRATOR_REF` pinned, `pico workspace validate` for each
COMPLETED task, and `tools/benchmark_metrics.py`, and writes `report.json` (completion, models, tokens,
calls, duration and the reported cost with unpriced participants; unobserved values are null). A missing
`pico` or `pi` exits 3. It never pushes, opens a PR or merges; without GitHub it covers the first wave,
and `--continue` repeats launch, validate and metrics after the operator published and promoted tasks in
sandbox repositories. When a real run and the deterministic suite disagree, the real run wins and a
scenario is added.

### GitHub synchronization: a safe `gh`-CLI boundary

`tools/github_sync.py` is the ONLY module in this repository that invokes
the authenticated `gh` CLI; `tools/backlog.py` is the only caller of its
mutating functions, applying a deterministic close-guard first. Ordinary
workers spawned per an execution plan have no code path to either module and
must never be given one — they must not close issues, edit issue metadata,
push branches, or merge PRs. No GitHub credential or token is ever read,
stored, or forwarded by this repository's code; `gh` resolves its own auth.

Safety properties of `tools/github_sync.py`, enforced at a single internal
choke point (`_run`):

- `shell=False` always; every invocation is an explicit argv list, never a
  shell string.
- A fixed allowlist of `(noun, verb)` pairs: `repo view`, `issue list`,
  `issue view`, `issue create`, `issue edit`, `issue close`, `issue reopen`,
  `issue comment`, `label list`, `label create`, `pr view`, `pr list`,
  `pr create`, `pr comment`, and `auth status`. No `gh pr edit`, no
  `gh pr merge`, no `gh pr checkout`, no `gh pr ready`, and no caller-supplied
  arbitrary flags — every wrapper function builds its own fixed argv shape.
- Generic `gh api` is not allowlisted. The one exception is the PR title/body
  edit performed by `edit_pr`, which uses the REST pulls endpoint because
  `gh pr edit` fails on some `gh` versions (e.g. 2.45) while querying
  deprecated classic Projects over GraphQL:
  `gh api -X PATCH repos/<owner>/<repo>/pulls/<n> -f title=<title> -f body=<body>`.
  Only supplied fields are sent (title, then body; at least one). The
  allowlist accepts that whole argv shape only: exact `-X PATCH`, an endpoint
  that fully matches `repos/<owner>/<repo>/pulls/<positive number>` with a
  validated `owner/repo` (or, when no repository is given, exactly the
  `{owner}/{repo}` placeholders that `gh` resolves from the current
  directory), and one or two unique `title`/`body` fields passed with `-f`
  (raw strings, so values such as `@file`, `true`, `null` or numbers are
  sent verbatim, never typed or read from files). The title must be
  non-blank; the body may be empty. Any other method, endpoint, query
  string, URL, placeholder, key, duplicate or flag (including typed `-F`)
  raises `GithubSyncError` before a subprocess is started.
- `gh` unavailable, not authenticated, and malformed/non-JSON output are all
  distinct, explicit error types (`GhUnavailableError`, `GhAuthError`,
  `GhOutputError`, `GhCommandError`, `GhTimeoutError`), never silently
  swallowed.
- Private repositories are supported identically to public ones (`gh`
  resolves this transparently); `repo_view()`'s `isPrivate` field lets
  callers branch on it if needed.

`tools/backlog.py` composes this boundary with `backlog/backlog.yaml`:

- `github_sync_status()` — read-only: is `gh` installed, authenticated, and
  which repository does it resolve to.
- `github_sync_task(id)` — read-only comparison of a backlog item against
  its associated issue; never mutates either side.
- `github_push_task(id)` — creates the issue if none exists yet, otherwise
  reconciles title/labels/open-closed state. **Close-guard**: an issue is
  only ever closed here when the local item's `status` is deterministically
  `completed` — `github_push_task` is the sole caller of
  `github_sync.close_issue` in this repository.
- `github_pull_task(issue)` — refreshes the cached sync snapshot and mirrors
  title/labels from GitHub, but deliberately **never changes a backlog
  item's `status`**: GitHub's open/closed state is a coarser, human-facing
  signal that must not silently drive workflow state. A remote-closed/
  local-not-completed mismatch is reported as a `discrepancy`, not
  auto-repaired.
- There is no `github-pr-create` (removed in the #39 fix): it called
  `gh pr create` with any base and head and no permission check, dry run or
  plan hash. PRs are opened by `github_integration --publish`, and
  `github_pr_link_task` links the result.
- `github_pr_link_task(id, pr)` — records an existing PR mapping and PR sync
  baseline under the same `review`/`completed` plus mapped-issue guard,
  without editing remote PR metadata.
- `github_pr_sync_task(id)` — read-only comparison of the linked PR against
  its cached baseline; it never mutates local state or GitHub.

Conflict semantics: each backlog item may carry a `github_sync` snapshot
(the last known synchronized remote title/state/labels). If both the local
item and the remote issue have changed independently since that snapshot to
*different* values, `github_sync_task`/`github_push_task`/`github_pull_task`
raise a conflict (`SyncConflictError`) and perform **no mutation on either
side** — the caller must resolve it explicitly. Neither side is ever
silently overwritten.

### How to validate (backlog/provenance/GitHub sync)

```sh
python3 tools/backlog.py validate
python3 -m unittest discover -s tests -p 'test_*.py'
```

`tests/test_backlog.py` covers schema validation, unique-id enforcement,
every valid/invalid transition, dependency validation, circular-dependency
detection, and the completed/ready dependency gate. `tests/test_provenance.py`
covers run-record schema validation, worker attribution fields, and explicit
`null` handling for unobservable model metadata. `tests/test_github_sync.py`
covers the `gh` allowlist/`shell=False` boundary, `gh` unavailable/not
authenticated/malformed-JSON/private-repo handling, and the composition
layer's conflict semantics and issue-close guard — **entirely mocked**;
these tests never invoke the real `gh` binary or touch a real repository.


## Runtime installation, state, reproducibility, and platform contract

The cached runtime is intentionally split into immutable code and mutable state.

- Code checkout: `~/.cache/pi-coding-orchestrator/repo` by default.
- Runtime state: `~/.local/state/pi-coding-orchestrator` by default.
- User model configuration: `~/.config/pi-coding-orchestrator/models.yaml` by default.

`scripts/use-latest.sh` exports `PI_ORCHESTRATOR_STATE_DIR` for cached
`pico` launches. `tools/provenance.py` resolves run storage through that
variable while continuing to load schemas from the exact orchestrator checkout
being executed. This prevents runtime traces from dirtying the cached Git tree
or blocking safe updates. Direct developer use without the environment
variable retains repository-local `provenance/runs` behavior.

Legacy untracked cached `provenance/runs/run-*.json` records are migrated into
the state directory before the cache cleanliness check. Any other untracked or
tracked modification still blocks an ordinary update.

### Project pinning

`pico pin <target> [ref]` writes the resolved full commit SHA to
`<target>/.pi/orchestrator.lock`. The lock is intended to be version-controlled
with a scientific software project when reproducibility matters.

Resolution precedence is:

1. explicit `PI_CODING_ORCHESTRATOR_REF`;
2. valid project `.pi/orchestrator.lock`;
3. remote default branch.

`pico run`, `pico setup`, `pico sync`, `pico watch`, `pico exec`, and
`pico doctor` honor the project lock. `pico update <target>` is the explicit
operation that resolves the configured update ref and advances an existing
lock to that commit.

The lock comes from the target repository, which may not be trusted, so the
launcher reviews it before building or running its runtime:

- **Release history.** A project pin runs only when it is an ancestor of the
  remote default branch (`refs/remotes/origin/HEAD`) or a tag contains it. A
  commit that exists only on another branch fails closed with
  `PIN_NOT_ON_RELEASE_HISTORY` before a runtime is materialized. The check
  first uses the cache's local refs, so a cached pin that they already show on
  release history runs offline as before. Otherwise the launcher refreshes the
  remote refs once and checks again; when that refresh fails (offline), the
  pin is refused with that reason in the message.
  `PI_CODING_ORCHESTRATOR_ALLOW_UNREVIEWED_PIN=1` runs such a pin anyway, with
  a warning. `pico doctor` reports the refusal and diagnoses with the default
  branch instead.
- **Approval.** `pico pin <target> [ref]` and `pico update <target>` record the
  written commit as the user's approved pin for that target checkout under
  `$STATE_DIR/approved-pins/` (keyed by the checkout's physical path). When
  the lock names a different commit, or no approval was recorded on this
  machine, the launcher prints a non-fatal warning naming the commit and the
  `pico pin <target> <sha>` command that approves it. An approved pin skips
  the release-history check: pinning an unreleased commit explicitly is the
  user's choice for that checkout only.
- An explicit `PI_CODING_ORCHESTRATOR_REF` or `pico pin <target> <ref>` is
  the user's own choice and is not reviewed.

Tags are matched by containment, not verified: the launcher does not check
tag or commit signatures yet, so a pin is only as trustworthy as the
repository's default branch and tags.

### Readiness interface

`tools/doctor.py` is the read-only readiness implementation behind
`pico doctor`. It reports:

- orchestrator revision and state directory;
- project pin;
- Git, Python, Pi, GitHub CLI, and Claude Code availability/version;
- GitHub and Claude authentication;
- target Git-repository validity;
- effective model profiles and model-configuration validity.

`pico doctor <target> --json` emits a stable schema-versioned JSON object for
CI and higher-level agents. It performs no model inference.

### Recovery and uninstall

`pico reset-cache --force` deletes and reclones only the cached code checkout.
The persistent state directory is not removed.

`pico uninstall` removes only the installed launcher. Optional
`--purge-cache`, `--purge-state`, and `--purge-config` flags are explicit
destructive cleanup operations.

### Supported launcher platforms

The v1.0 launcher contract is POSIX shell on Linux and macOS. Windows users
should use WSL2 (recommended) or a compatible Git-Bash environment. Native
PowerShell is not a supported launcher surface in v1.0. The full CI suite runs
on Ubuntu and macOS to enforce the supported POSIX contract.
