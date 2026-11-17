# Provider request budgets (controlled API pilot)

`tools/provider_budget.py` implements admission **before each HTTP attempt**.
Headless execution, workspace launches and the scheduler select it when
`PICO_PROVIDER_BUDGET_CONFIG` and the aggregate ledger are configured. An
unconfigured installation keeps its existing behavior. Interactive `pico run`
and `scripts/run-local.sh` are unsupported in this mode and refuse configured
provider budgets; use the headless paths or explicit gateway wrapper. A provider configuration
without both token and dollar limits in at least one common ledger scope is
refused. Without the provider configuration, non-launch ledger dimensions
continue to fail closed.

Interactive `pico run` and `scripts/run-local.sh` refuse when either budget
environment variable is set. Use `pico exec start` or workspace launch/schedule
for capped orchestration. Plain Pi outside these entry points does not acquire
the gateway automatically.

This is a cooperative local runtime boundary, not an OS/network sandbox against
hostile code. The gateway owns the real provider API keys; Pi, Claude Code and
workers receive only a fresh local proxy credential. HOME, XDG directories,
Pi/Claude/Codex auth directories are fresh and isolated. Project Pi extensions and
configuration, discovered extensions and MCP connections are disabled; only
explicitly reviewed extension paths and PICO's own launch extensions load.
Project `.pi/claude-bridge.json` overrides are refused at startup and by an
always-injected guard before every Claude tool invocation, including outside
a parent run and in worker sessions. The generated Claude executable wrapper
forces empty settings sources, disables hooks, and refuses explicit external
or unsafe settings; it preserves the reviewed worker sandbox settings. The
headless record retains the original binary identity and the child pin records
both original and wrapper identities. Workers with arbitrary filesystem/shell access are trusted not to retrieve
credentials elsewhere or change the ledger/config/runtime. Use a separately
confined OS account/network policy if that stronger boundary is required.

## Supported paths

| Path | Supported mechanism |
| --- | --- |
| Pi lead, final answer, compaction | Approved models in isolated `models.json` use the local HTTP gateway |
| Pi Codex-family worker | `openai-codex/<id>` is explicitly configured as `openai-responses` using an API key, not ChatGPT OAuth |
| Pi worker timeout summary | Its direct `completeSimple` uses the same approved model URL and local credential |
| ClaudeWorker and AskClaude bridge | API-key Claude Code receives `ANTHROPIC_BASE_URL` and isolated auth; every nested message goes through the gateway |
| Client/SDK retry | Each incoming HTTP attempt receives a fresh ledger operation ID; caller idempotency keys never waive admission |
| Unsupported provider/model/feature | Rejected before forwarding; no fallback or model downgrade |

The gateway supports only POST OpenAI Responses and Anthropic Messages with
explicit text/local function context, standard service tier, bounded standard
reasoning, and output limits. Subscription/OAuth, Bedrock/Vertex/Azure, batch,
websocket, hosted tools/search, server-managed history/compaction, pro reasoning,
media and extended OpenAI cache retention are unsupported. New CLI features
can therefore cause a safe refusal until reviewed. Claude's `?beta=true` route
is accepted, but beta headers are not forwarded. The gateway sends only its own
fixed authentication/content/API-version headers to fixed HTTPS provider hosts.
There is no upstream retry or redirect following.

Install and list reviewed plugins explicitly in configuration `extensions`:
`/absolute/path/pi-subagent-workflows/extension/index.ts` for Codex workers and,
when using Claude routes, the extension that actually registers AskClaude.
Legacy combined bridge 0.7.0 uses `pi-claude-bridge/src/index.ts`; provider-only
bridge 0.11.0 moved that tool into `pi-claude-bridge-askclaude`. Companion 0.1.1
was verified with Pi 1.3.0 in the offline fixture; its Pi peer range is >=0.86.1.
Companion 0.1.2 requires Pi >=1.5.1 and is not a substitute for that verified
combination. Pin compatible plugin versions with a lockfile. Run the optional
real-plugin fixture against the exact selected
extension before enabling it; a provider-only bridge does not supply AskClaude.
There is no automatic package
discovery. A missing required worker tool or unpriced route remains blocked by
the existing routing/preflight guards. Those guards and mutation permissions
are unchanged. A lead-only smoke can use `extensions: []`; normal orchestration
requires its worker plugins. Arbitrary external extensions are outside the
validated transport set and must be independently reviewed.

## Reservation and settlement

Each request reserves the **entire published model context window** as input,
plus its enforced output maximum, before opening the upstream connection.
This intentionally avoids using a tokenizer estimate as a hard bound. The
provider contract rejects inputs beyond the context window. `max_tokens` or
`max_output_tokens` is reduced to the configured limit before forwarding;
reasoning tokens are included in the supported API's output limit.

The dollar reservation applies the largest input/cache-read/cache-write rate
to every input token, including long-context rates, and the output ceiling to
every output token. All prices are decimal USD per million tokens. Complete
terminal usage permits settlement to measured tokens at those same worst-case
rates. Anthropic input, cache read and cache creation are summed; Responses
`input_tokens` already contains cached input. Thus ledger `actual.cost_usd` is
a conservative **pricing-model charge**, not the actual provider invoice.
It excludes taxes, subscriptions and unrelated account spend.

Missing categories, incomplete streams, client disconnects, provider errors,
timeouts and crashes retain the entire reservation. No automatic expiry or
release exists. The ledger shares task/attempt/day/lifetime checks across
processes. Uncertain reservations count on every later day; complete cross-day
requests count on both admission and settlement days. A duplicate internal
operation ID never authorizes forwarding again. Measured overruns remain visible
and consume future capacity; they are never clamped or hidden.

Launch records use `launches=1, requests=0, tokens=0, cost_usd=0`; HTTP records
use `launches=0, requests=1`. Task and attempt IDs come from headless execution's
existing aggregate task identity and execution ID, so resumes accumulate and
new attempts cannot reset task/lifetime limits. Directory fsync failures now
prevent successful admission; parent-path aliases share a canonical lock and
hard-linked ledger files are rejected. A local POSIX filesystem with working
locking, fsync and atomic rename is required.

## Reviewed catalog and pricing assumptions

The built-in catalog was reviewed **2026-11-17** and expires after **2026-11-24**.
Unknown model IDs, understated context windows or prices, and output ceilings
above the published maximum are refused. Configurations may raise input/price
reservations and lower output ceilings. Their SHA-256 digest is pinned in the
ledger; changing it cannot reuse the same allowance. Review a catalog update
before extending the validity window. Expiry is checked when loading a configuration and before each request; this does not certify that a provider cannot change a mutable
model alias or price during the review window.

An explicit **unused-ledger recovery** command can replace that pin if provider
metadata preflight found the original model unavailable and no operation has
ever reserved capacity. It holds the same exclusive lock as admission, compares
the caller's expected old digest, and refuses every nonempty reservation map,
including settled or zero-usage launches. It preserves all existing limits and
records the previous/new contract, timestamp and reason in an append-only
`provider_contract_history`. This is not a budget reset or a release operation.

```sh
python3 tools/provider_budget.py rebind-empty \
  --config /absolute/private/reviewed-replacement.json \
  --ledger /absolute/private/existing-ledger.json \
  --expected-contract OLD_SHA256_FROM_LEDGER_STATUS \
  --reason 'Approved provider change after no-spend metadata preflight' \
  --attempt-cost-usd 15
```

`--attempt-cost-usd` is optional and requires explicit authorization. It can
change only an existing attempt dollar ceiling, never exceed the existing
lifetime dollar ceiling, and records its old/new values. Every other limit is
preserved exactly. For example, changing an unused attempt ceiling from $10 to
$15 leaves an existing lifetime $30/8M-token/200-request/6-launch ceiling intact.
No general limits replacement API is provided. The replacement configuration
is validated without inference or access to API credentials. Stop any gateway
from an older runtime before recovery; current gateways verify the pinned
contract atomically on every request, so an idle gateway holding the old
configuration is refused after the change. Replaying a stale expected digest
cannot change the ledger again.

The generated native Pi entry for `claude-opus-5-5` explicitly retains
`compat.forceAdaptiveThinking`. This makes `--thinking high` send adaptive
thinking and `output_config.effort=high`; it does not translate to a legacy token
budget or silently discard effort. An offline real-Pi/fake-upstream regression
checks the exact model ID, those fields, output clamping and complete settlement.

| Model | Input context bound | Output maximum | Input | Output | Cache read | Cache write |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| gpt-6-luna | 1,050,000 | 128,000 | 0.20 | 0.75 | 0.02 | 0.25 |
| gpt-6.1-sol | 1,050,000 | 128,000 | 4 | 15 | 0.20 | 5 |
| gpt-6-astra | 1,050,000 | 128,000 | 20 | 75 | 2 | 25 |
| claude-opus-5-5 | 1,000,000 | 128,000 | 4 | 20 | 0.2 | 8 |

These are conservative standard-mode ceilings, including applicable long-context
multipliers and cache creation. Sources: [OpenAI model comparison](https://developers.openai.com/api/docs/models/compare),
[OpenAI pricing](https://developers.openai.com/api/docs/pricing),
[Astra long-context pricing](https://developers.openai.com/api/docs/models/gpt-6-astra),
[Opus 5.5 limits and rates](https://platform.claude.com/docs/en/models/opus-5-5/overview),
[Anthropic cache pricing](https://platform.claude.com/docs/en/about-claude/pricing),
[Anthropic context accounting](https://platform.claude.com/docs/en/build-with-claude/context-windows),
[Responses output limit](https://developers.openai.com/api/reference/resources/responses/methods/create),
and [Claude gateway support](https://code.claude.com/docs/en/llm-gateway).
Regional endpoints and caller tier/location headers are not forwarded.

## Configure an explicitly authorized pilot

Copy `templates/provider-budget-openai.json` to a reviewed absolute config path.
It selects Luna as lead and includes the three current Codex-family capacities;
select a different lead explicitly if required. Add the absolute worker plugin
path described above. This changes API transport only: task routing and capacity
selection remain governed by PICO's model/routing configuration. The gateway
does not alter those profiles to make an unsupported route run.

Provide `OPENAI_API_KEY` to the gateway parent through the existing secure secret
delivery mechanism. No key belongs in the JSON file. Claude requires a separate
reviewed model entry and `ANTHROPIC_API_KEY`. Only configured providers require
keys; OAuth-only installations refuse before the child launches.

For example, after separately approving these limits:

```sh
python3 tools/budget_ledger.py init /absolute/private/pilot-ledger.json \
  --limits '{"attempt":{"tokens":4000000,"cost_usd":"10"},"lifetime":{"tokens":8000000,"cost_usd":"20"}}'
export PICO_AGGREGATE_BUDGET_LEDGER=/absolute/private/pilot-ledger.json
export PICO_PROVIDER_BUDGET_CONFIG=/absolute/private/provider-budget.json
python3 tools/headless_run.py start --target /absolute/target --repo owner/repository \
  --execution-id bounded-pilot --objective 'The explicitly approved bounded task'
```

Existing mutation authorization rules still apply. A direct Pi smoke can use
`python3 tools/provider_budget.py --config CONFIG --ledger LEDGER --task TASK
--attempt ATTEMPT -- pi --print 'bounded prompt'`; this direct wrapper does not
consume a headless launch slot. Reuse the same ledger, never recreate it between
arms of a comparison. With a 4,096 output limit, a Luna request requires about
$0.266 of available conservative reservation, Sol about $5.312, and Astra about
$26.557. A smaller allowance correctly refuses those larger requests even when
the actual short prompt would be cheaper. Full-context reservation is safe but
can strand capacity; it is deliberately not marketed as accurate invoice prediction.

## Offline validation

`tests/test_provider_budget.py` uses a real local HTTP gateway and fake upstream
connections to test pre-send admission, concurrent exhaustion, retries, output
clamping, cache categories, incomplete receipts, unsupported options and auth
isolation. An installed Pi fixture covers a real lead, fresh worker SDK session
and direct timeout-summary transport. When Claude Code is installed, a fixture
drives its nested local-tool turn and final response through the fake upstream.
These tests spend no model tokens and do not prove live provider entitlement or
billing. Existing ledger tests cover process crashes, day rollover and aggregate
scope accumulation; existing launch/workspace tests retain the no-adapter refusal.

`tests/test_provider_workflows_integration.py` preserves optional tests of the
actual installed AskClaude and subagent plugins, including the real hard-timeout
summary. Its module documentation lists the explicit plugin-path variables and
timeout opt-in. With those variables absent it skips clearly; with them set it
still uses only fake local upstreams and fake credentials.
