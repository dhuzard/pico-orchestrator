# Paired launch-brief pilot

This is a local, one-line arithmetic task with an acceptance check outside the target repository. Both arms use the same runtime revision, task, deterministic base commit, policies, model configuration, lead model and thinking level. Only the launch brief differs. The selector is opt-in and recorded in the execution; all normal lifecycle, routing, worker, review, isolation and budget guards remain active. Neither arm publishes. The task can still fail, stop at a limit, or have missing usage data.

The fixture's remote identifies a fictitious sandbox repository. `--no-access-check` is necessary because it has no GitHub repository; this skips the remote accessibility check only. Local Git identity and all other guards remain in force. Local worktrees and commits require `--allow-mutation`, passed by the runner. No push, PR or merge to a remote is allowed by the fixture policy. `acceptance.py` checks the exact file contents and the complete tracked diff from the base; PICO validates the completed branch through its normal workspace validation. Protect the harness files themselves and compare their hashes, because the target has the same OS user as the harness.

## Before any paid run

A real run needs a reviewed, working provider gateway that enforces every provider request, API-key credentials for its supported providers, a pinned provider rate/config file, a single durable aggregate ledger, and an explicit total spending authorization. OAuth/subscription access does not establish a dollar cap. The workspace token budget is a launch gate, **not** an in-flight provider cap. If a backend or route cannot be covered, stop and report that limitation. The gateway may conservatively reserve a full context/output allowance for each request; a small cap may refuse the first request. Do not bypass it or reset its ledger. Failed/uncertain requests still consume the ledger as specified by the gateway.

Create the provider configuration and ledger using the gateway's documented instructions. The ledger must have lifetime and attempt token and dollar limits; the lifetime limit covers **all** arms, including failed attempts, and must not exceed the authorized total. The same ledger and provider config must be used throughout. The maximum authorized exposure is the ledger lifetime cap, not the sum of independent per-phase allowances. The per-attempt limit must accommodate the largest admitted request; a pilot with too small a limit can only show safe refusal. Pin worker model profiles and provider prices, with source/date evidence; never place keys in config, command-line arguments or reports.

`run.py` refuses without `PICO_REAL_BENCHMARK=1`, the gateway, an existing suitable ledger and provider config, a full known revision, real launchers and a fresh output directory. It has no resume, publication, or budget-override option. The lead model and effort are explicitly passed to Pi, with selector fields pinned to the execution record. Worker models must also be pinned in the supplied model configuration. The runner records observed usage separately from configured pins. API credentials must be supplied securely to the gateway's environment as documented there.

Use a clean pinned runtime checkout and ensure the `pico` launcher actually resolves that revision. Keep the model configuration, Pi settings, backends, rate card, credentials/route, environment, and harness revision the same for all arms. Check that the provider gateway and ledger integration are covered by their offline tests before proceeding. Provider model and effort support must be checked; a configured Pi flag is not proof of the provider's effective effort. Keep output directories outside Git repositories. The runner records local paths and run records: review for sensitive data before sharing.

For each pair, use the same ID and common ledger/config/model config. With multiple pairs, predeclare and alternate the arm order (pair 1 baseline then candidate, pair 2 candidate then baseline, etc.) to reduce order/cache bias. Do not choose the order based on early outcomes. Example, with limits and values replaced after authorization:

```bash
export PICO_REAL_BENCHMARK=1
export PICO_PROVIDER_BUDGET_CONFIG=/absolute/path/provider-config.json
export PICO_AGGREGATE_BUDGET_LEDGER=/absolute/path/shared-ledger.json
REV=<full-40-character-runtime-commit>
MODEL=<exact-provider/model>
EFFORT=<supported-thinking-level>
MODEL_CONFIG=/absolute/path/models.yaml
OUT=/absolute/path/outside-git
python3 benchmarks/efficiency/run.py "$REV" pair-1 baseline "$OUT/pair-1-baseline" --max-tokens <workspace-gate> --max-lead-calls <limit> --duration <seconds> --lead-model "$MODEL" --lead-effort "$EFFORT" --model-config "$MODEL_CONFIG"
python3 benchmarks/efficiency/run.py "$REV" pair-1 candidate "$OUT/pair-1-candidate" --max-tokens <workspace-gate> --max-lead-calls <limit> --duration <seconds> --lead-model "$MODEL" --lead-effort "$EFFORT" --model-config "$MODEL_CONFIG"
```

Stop if the gateway, ledger, identity, model or policy evidence is inconsistent. Preserve interrupted state and unknowns rather than retrying under a fresh ledger. Inspect the execution and run records, provider accounting, exact validated commits and checks. Check that all expected workers actually used the configured models and independent roles. Do not infer a successful run from the script's exit status alone. Lead tokens, worker tokens/calls, elapsed time, cost coverage and checks are recorded in each report. Model-reported estimated cost and the gateway ledger are separate. The gateway settlement cost is a conservative rate-card ceiling for measured tokens, not a provider invoice or the actual amount billed. The report includes unsettled entries; do not claim complete provider coverage from settled entries alone, because every backend must also be known to use the gateway. Unpriced or unobserved costs/tokens are not complete measurements.

To analyze, write a manifest alongside the output folders:

```json
{"version": 1, "samples": [{"id": "pair-1", "baseline": {"report": "pair-1-baseline/report.json"}, "candidate": {"report": "pair-1-candidate/report.json"}}]}
```

```bash
python3 benchmarks/efficiency/analyze.py "$OUT/manifest.json" --output "$OUT/comparison.json"
```

Analysis checks matching recorded fixed inputs, selector evidence and the observed lead model. It excludes a pair's deltas unless both tasks completed and passed the acceptance check; each metric also requires complete data. It reports the number of usable paired observations, mean, sample standard deviation and range; one pair is a smoke test, with no estimate of variability or reliable causal claim. Worker model and effort observations must be reviewed separately. The lead effort is labeled unobserved unless the gateway records explicit request evidence; it does not gate descriptive lead-call counts. Provider contract ceilings exclude launch-only ledger entries and are null when observed request entries are missing, malformed or unsettled. Unobserved metrics are null, never zero. Even repeated samples are sensitive to provider/model drift and cache state.

Offline checks (no model calls):

```bash
python3 -m pytest -q tests/test_efficiency_benchmark.py tests/test_headless_run.py
```
