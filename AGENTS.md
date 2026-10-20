# AGENTS.md — PICO / Pi Coding Orchestrator

## Purpose
PICO is a repository-independent **development orchestration** harness for Pi with deterministic Claude/Codex task routing, run provenance, worktree-scoped workers and Pico Garden observability. It is not an autonomous scientific interpretation engine and it must not silently take control of repository governance.

## Authorities
Read `README.md`, `docs/technical-reference.md`, `docs/orchestration-guidance.md`, `docs/work-modes.md` and the relevant versioned schemas.
- `routing/default-policy.yaml` and `routing/delegation-policy.yaml` own policies. `tools/routing_engine.py` is the single evaluator implementation.
- `schemas/` own task, mission, execution and provenance contracts. Do not duplicate route logic in prompt templates or extensions.
- `extensions/`, `tools/`, `scripts/` implement the runtime and installation. `tests/` and `benchmarks/` validate behavior.
- `provenance/` and local state are records of specific runs, not a place for synthetic approval or fabricated metrics.

## Agent boundaries
- No blanket inference of project permissions from a working directory. Resolve repository Git identity and explicitly scope each worker and its worktree. Ambiguous/multiple remotes, unresolved owner, inconsistent branch or unverified workspace ownership must fail closed.
- A worker may mutate only its assigned checkout/worktree, within a reviewed wave and policy; code integration is serialized, and any cross-repository plan requires explicit workspace routing rather than bypassing per-repo safeguards.
- Claude/Codex routing is a *policy decision*, not an evidence claim that one model is always superior. Preserve benchmark provenance and the distinct specifier/implementer/reviewer roles.
- Do not auto-merge, publish, force-push, change secrets/permissions, delete branches, deploy or alter target data without explicit user authorization. Review agents should be independent of the producing implementation.
- Never invent token/cost metrics or assert that replay, model safety, or scientific quality is guaranteed. Unavailable backends and interrupted runs should be explicit.

## Checks
```bash
npm ci
npm run check
python -m pip install -r requirements-dev.txt pytest
python -m pytest -q tests
```
For routing changes also run the project's policy/schema validation and offline routing-case benchmarks documented in `docs/technical-reference.md`. Test ambiguous repo identity, concurrent waves, resume, worker isolation and safe failure. Do not claim any external agent deployment was tested without doing so.
