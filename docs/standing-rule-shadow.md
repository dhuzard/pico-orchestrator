# Standing-rule shadow evaluation (stage 1)

`tools/standing_rule.py` evaluates a proposed documentation-edit rule against
explicit offline snapshots. It **cannot authorize or start execution**. It is
not connected to the issue dispatcher, headless launcher or GitHub publication
adapter. Existing exact-issue interactive approval remains unchanged.

The intended eventual workflow is a bounded correction to guide prose ending
in a private draft PR. Stage 1 supplies only a schema, deterministic structural
admission decisions and traceable refusal evidence. A successful shadow result
is not evidence that any edit, model run, approval or PR happened.

## Contract and use

`schemas/standing-rule.schema.json` owns the closed rule contract and its
`candidate` and `context` definitions. Unknown properties and missing fields
fail validation. `enabled` must explicitly be `false`; `true` is invalid.
There is no `--apply`, `--approve`, activation or dispatch option.

For previously prepared, trusted local JSON snapshots:

```sh
python3 tools/standing_rule.py \
  --rule /path/to/proposed-rule.json \
  --candidate /path/to/candidate.json \
  --context /path/to/observed-context.json \
  --now 2026-11-13T10:00:00Z
```

The explicit UTC clock makes replay deterministic. Exit 0 means structurally
eligible in shadow mode; exit 2 means refused or invalid input. Every verdict
contains `authorized: false` and `execution_enabled: false`. Nothing is written
or reserved. Input-load errors do not echo file contents.

The pure Python API is `evaluate(rule, candidate, context, now=utc_datetime)`.
`queue_content(rule, candidate)` renders a canonical task draft using the
existing dispatcher's three headings. It does not create an issue. Use
`issue_digest(snapshot)` for the shadow manifest's title/body digest; this
canonical JSON digest is deliberately separate from the legacy dispatcher's
exact-approval digest. Neither is an approval signature.

The synthetic `fixtures()` helper in `tests/test_standing_rule.py` is a complete
example. Its identities, policies, budget observations and dates are invented
for offline tests, not real activation records.

## What is checked

- Rule/schema version, exact task kind, finite validity window of at most seven
  days, current UTC day and recent snapshots (at most 60 seconds old).
- Separate queue and target repository IDs, matching names, private non-fork
  status, explicitly allowed numeric source and queue author IDs, open issues,
  selectors and issue content digests. A label alone cannot confer authority.
- Canonical queue content bound to typed source/task data, plus compatibility
  with the existing dispatcher's three-heading and length contract. Arbitrary
  queue objectives remain ineligible even if their digest is recomputed.
- Declared clean checkout and verified identity, default branch, runtime SHA,
  policy/guidance digests and the required worktree/commit/push/draft-PR policy
  permissions. A proposal can narrow policy, not add missing permissions.
- Revocation, active job count, prior source attempts, per-day/lifetime attempt
  counts, consecutive failures and declared accounting status. At most three
  attempts, one daily and two consecutive failures fit this first schema.
- A proposed full per-run reservation against remaining daily/lifetime budgets,
  with integer USD cents, capped at 200/run, 200/day and 600/lifetime. Zero budget
  rejects admission. Committed amounts include spend and outstanding
  reservations; these are caller-provided observations, not measured spend.
- Up to two explicitly allowed existing `docs/guide/*.md` paths; no traversal,
  absolute paths or backslashes. Regular non-executable files and exactly one
  occurrence of each bounded quoted text are required. Duplicate paths,
  obvious role/control framing and excessive estimated patch size are refused.

Provenance records the rule, candidate and context digests; repository/issue
IDs; base/runtime commits; policy/guidance digests; and the explicit clock.
It records `authorization_kind: none` and
`evaluation_kind: standing-rule-shadow`. Raw issue bodies, quoted prose and
file contents are not echoed in verdicts. Stable `STANDING_*` codes identify
refusals; validation errors include field locations without field values.

## Limits that remain blocking

All observations are supplied to a pure function. This evaluator does not
contact GitHub, authenticate authors/bridge signatures, verify repository Git
state, fetch a default-branch policy, reserve budget, enforce OS isolation or
sign an operator approval. A caller lying about its snapshots can produce an
eligible shadow result, which still authorizes nothing. Never accept these
snapshots from queue text as trusted observations.

Task admission here is **structural**, not a semantic proof that prose is safe.
The evaluator does not parse Markdown nodes, detect scientific/legal meaning
changes, verify final patch bytes, independently review output, or authorize a
publication plan. Estimated diff limits are admission hints only. A future
trusted validator must check the actual diff; a future publisher must reconstruct
the verified patch in a clean checkout and enforce the exact draft-PR plan.

Standing execution must remain disabled until a reviewed follow-up implements:

1. Operator-controlled activation/revocation with authenticated, fresh remote
   observations and content-bound grants distinct from human exact approval.
2. Durable atomic attempts/budget reservations and source deduplication shared
   with the existing dispatcher's global execution lock and crash recovery.
3. Enforced capability limits and controller/lead/worker/publisher isolation,
   including credential and network boundaries for **all** model backends.
4. Aggregate monetary enforcement for lead and workers, supervised revocation
   and limits, and no automatic resume or new execution retry.
5. Actual prose-only patch validation, independent review and restricted
   publication through the existing GitHub adapter.

Do not implement any of these by writing synthetic rows into the current
approval database or using `--allow-mutation` to bypass the existing contract.
No runtime pin should be changed merely because shadow evaluation passes.

## Offline verification

```sh
python3 -m pytest -q tests/test_standing_rule.py tests/test_issue_dispatcher.py
```

The tests cover eligible fixtures, rejection boundaries, malformed/forged data,
revocation and budget exhaustion, deterministic provenance, no side effects,
the absence of an apply mode and legacy dispatcher's approval behavior. They
use no live GitHub requests or model calls. Repository-wide validation remains
as specified in `AGENTS.md`.
