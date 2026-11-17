# Aggregate budget accounting

The optional durable ledger enforces **attempted launch admission counts** at
headless and workspace launches, including dispatcher jobs, scheduler launches,
resumes and new attempts. All use the central headless admission callback.
It is disabled unless a ledger path is explicitly configured. These changes
create no live ledger and activate no spending policy.

Create a ledger with fixed limits, then select it for the launches that must
share those limits:

```sh
python3 tools/budget_ledger.py init /absolute/path/budget.json \
  --limits '{"attempt":{"launches":2},"task":{"launches":3},"day":{"launches":6},"lifetime":{"launches":20}}'
python3 tools/workspace_scheduler.py workspace.yaml \
  --aggregate-ledger /absolute/path/budget.json
python3 tools/budget_ledger.py status /absolute/path/budget.json
```

`workspace_run.py launch` accepts the same `--aggregate-ledger` option. The
`PICO_AGGREGATE_BUDGET_LEDGER` environment variable supplies the default when
the option is absent. All participating launches must select the same ledger.
Changing files or removing that configuration is outside this cooperative
local admission boundary; this is not protection against a hostile operator
or a compromised worker that can modify the ledger.

Direct execution also accepts `pico exec start --aggregate-ledger PATH`.
Dispatcher configuration can set `aggregate_ledger` to that same absolute path.
No configured ledger means this optional aggregate launch gate is off. Existing
per-execution time and call limits remain independent.

The ledger's `task` limit applies to a stable workspace/task identity across
attempts; `attempt` applies to one execution id, including all its resumes;
`day` means UTC admission day; `lifetime` spans every entry in that ledger.
An idempotent headless start that returns already running/finished creates no
admission. A real resume or retry consumes another admission. Once admission
is persisted, even a failed launch conservatively consumes its slot. No-op
starts, `--ignore-budget`, and the scheduler's observed-usage precheck cannot
reset or bypass a consumed aggregate allowance. Initialization refuses an
existing file. Missing, malformed or unsupported ledgers fail closed.

Every update holds a process-safe exclusive file lock and atomically replaces
an fsynced JSON file. These guarantees require a local filesystem with working
POSIX advisory locks and atomic rename. Outstanding reservations never expire
or release after a crash. They count against later days too because the
operation might still spend then. Cross-day settlements conservatively count
on both admission and settlement days. This can hold capacity longer than
necessary; there is deliberately no automatic reset or release command.

## Provider accounting boundary

The ledger API supports `requests`, `tokens` and `cost_usd` quantities as well
as `launches`. Decimal dollar arithmetic avoids float comparison errors.
`reserve(path, operation_id=..., task_id=..., attempt_id=..., maximum=...,
supported=...)` checks all configured scopes in one atomic transaction.
Only its `created=True` return authorizes a new external operation; replaying
an operation id returns the old reservation without authorizing another one.
The adapter must provide an actually enforceable conservative maximum for
every configured dimension before that operation begins.

`settle(path, operation_id, actual=..., complete=True)` releases unused
reserved quantities only with complete measured accounting for all dimensions.
Unknown/missing prices, timeouts or lost receipts must use `complete=False`,
which preserves the entire maximum. Settlements cannot be rewritten. An
observed overrun is recorded truthfully and included in subsequent admission
checks, never clamped to the reserved amount.

The opt-in [provider gateway](provider-budget.md) reserves request, token and
dollar ceilings before each supported API HTTP attempt, including nested
Claude calls and Pi worker/summary calls. Set `PICO_PROVIDER_BUDGET_CONFIG`
with the aggregate ledger to select it at headless/workspace launches. Without
that adapter, launch admission still refuses provider dimensions with
`AGGREGATE_BUDGET_UNSUPPORTED`. A lead-call work threshold cannot replace this
transport boundary. API credentials are required; subscription OAuth routes
are unsupported. Dollar figures bound a reviewed pricing model, not a vendor
invoice. Read the gateway documentation for the supported contracts and
cooperative runtime boundary.

Existing manifest `budget.max_tokens` and `budget.max_cost_usd` remain
observed-usage launch gates. Missing known linked telemetry blocks either;
missing pricing blocks the monetary gate. Running children can overshoot
these observed gates. Cache reads are excluded only from the workspace's
input + output + cache-write token-work metric. They remain part of reported
monetary estimates. A partial worker estimate is never a complete run total.

## Offline validation

`tests/test_budget_ledger.py` covers simultaneous processes competing for the
last slots, abrupt process exit after reservation, day rollover, missing-price
settlement, duplicate operation ids, resume/retry accumulation, malformed
ledgers, unsupported provider ceilings and measured overruns.
`tests/test_usage_accounting.py` covers missing/corrupt linked telemetry,
previous attempts, explicit cost completeness and cache-read monetary charges.
`tests/test_workspace_run.py` exercises real fake-provider launch admission,
no-op repeat starts and rejection before an unsupported provider ceiling can
launch anything. These tests use no model credentials or live provider calls.
