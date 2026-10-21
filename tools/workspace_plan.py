#!/usr/bin/env python3
"""Read-only planner for a workspace execution plan spanning several repositories.

A workspace manifest (schemas/workspace.schema.json) names repositories with an
explicit GitHub identity, a local checkout and a pinned commit, and tasks with
cross-repository dependencies. This planner resolves and validates every
repository, checks GitHub access, builds the dependency graph and prints
execution waves. It never creates a worktree, launches a worker, or mutates Git
or GitHub: its only subprocesses are read-only `git rev-parse`, `git remote`,
`git cat-file` and `gh repo view`.

The single-repository invariants are reused, not relaxed: each repository is
resolved through github_integration.resolve_target with the explicit identity
required, so an ambiguous checkout or an identity absent from the remotes fails
closed with the existing TARGET_REPOSITORY_* codes. Two entries pointing at the
same Git repository or the same GitHub identity also fail closed.

Exit codes: 0 PLANNED, 1 BLOCKED, 2 invalid invocation or unreadable manifest.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import github_integration  # noqa: E402
import github_sync as gh  # noqa: E402
import session_scope  # noqa: E402
import provenance  # noqa: E402

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency: jsonschema. Run: python3 -m pip install -r requirements-dev.txt") from exc

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schemas" / "workspace.schema.json"
PLAN_VERSION = 1
READINESS_STATES = ("planned", "implemented", "validated", "ready_for_downstream", "merged")
# Only these states satisfy a dependent: a PR existing (implemented) or passing
# tests (validated) is not evidence that the interface is stable.
UNBLOCKING_STATES = frozenset({"ready_for_downstream", "merged"})
# A qualification task checks validated interfaces before they are released,
# so it only needs its dependencies validated, not yet ready_for_downstream.
QUALIFYING_STATES = frozenset({"validated", "ready_for_downstream", "merged"})

AccessCheck = Callable[[str], dict]


class WorkspaceError(Exception):
    """Manifest cannot be read or validated at all (exit code 2)."""


def orchestrator_revision(root: Path = ROOT) -> str | None:
    marker = root / ".pico-runtime"
    if marker.is_file():
        value = marker.read_text(encoding="utf-8").strip()
        return value if re.fullmatch(r"[0-9a-f]{40}", value) else None
    try:
        probe = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], shell=False,
                               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = probe.stdout.strip()
    return value if probe.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", value) else None


def load_schema(path: Path = SCHEMA_PATH) -> dict:
    schema = json.loads(path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return schema


def load_manifest(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkspaceError(f"manifest is not readable: {exc}") from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkspaceError(f"manifest is not valid YAML/JSON: {exc}") from exc


def validate_manifest(manifest: Any, schema: dict | None = None) -> list[str]:
    validator = jsonschema.Draft202012Validator(schema or load_schema())
    errors = sorted(validator.iter_errors(manifest), key=lambda e: list(e.absolute_path))
    messages = [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in errors]
    if isinstance(manifest, dict) and isinstance(manifest.get("tasks"), list):
        for index, task in enumerate(manifest["tasks"]):
            if isinstance(task, dict) and isinstance(task.get("objective"), str) and provenance._contains_secret_like_content(task["objective"]):
                messages.append(f"tasks/{index}/objective: instruction looks like it contains a secret")
    return messages


def gh_access(repository: str) -> dict:
    """Verify the authenticated gh user can read the repository (read-only)."""
    if not gh.gh_available():
        return {"status": "unverified", "blocker_code": "GITHUB_ACCESS_UNVERIFIED", "detail": "gh CLI unavailable"}
    try:
        gh.require_authenticated()
        view = gh.repo_view(repo=repository)
    except gh.GhAuthError as exc:
        return {"status": "unverified", "blocker_code": "GITHUB_ACCESS_UNVERIFIED", "detail": str(exc)}
    except gh.GithubSyncError as exc:
        return {"status": "denied", "blocker_code": "WORKSPACE_REPOSITORY_ACCESS_DENIED", "detail": str(exc)}
    if str(view.get("nameWithOwner", "")).casefold() != repository.casefold():
        return {"status": "denied", "blocker_code": "WORKSPACE_REPOSITORY_ACCESS_DENIED",
                "detail": "gh resolved a different repository"}
    return {"status": "ok", "blocker_code": None, "detail": None, "private": bool(view.get("isPrivate"))}


def _commit_present(checkout: Path, sha: str) -> bool:
    try:
        probe = subprocess.run(["git", "-C", str(checkout), "cat-file", "-e", f"{sha}^{{commit}}"],
                               shell=False, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def resolve_repositories(entries: list[dict], *, base: Path, access_check: AccessCheck | None) -> tuple[list[dict], list[dict]]:
    """Resolve every manifest repository; blockers never stop the remaining entries."""
    resolved: list[dict] = []
    blockers: list[dict] = []
    seen_common: dict[Path, str] = {}
    seen_identity: dict[str, str] = {}

    def block(entry_id: str, code: str, detail: str) -> None:
        blockers.append({"scope": "repository", "id": entry_id, "code": code, "detail": detail})

    for entry in entries:
        item = {"id": entry["id"], "repository": entry["repository"], "path": entry["path"],
                "resolved_path": None, "git_common_dir": None, "pinned_commit": entry["pinned_commit"],
                "access": {"status": "skipped", "blocker_code": None, "detail": None}, "blocker_code": None}
        resolved.append(item)
        checkout = Path(entry["path"])
        if not checkout.is_absolute():
            checkout = base / checkout
        if not checkout.is_dir():
            item["blocker_code"] = "WORKSPACE_REPOSITORY_PATH_MISSING"
            block(entry["id"], item["blocker_code"], f"checkout directory does not exist: {entry['path']}")
            continue
        item["resolved_path"] = str(checkout.resolve())
        try:
            identity = github_integration.resolve_target(checkout, entry["repository"])
        except github_integration.IntegrationError as exc:
            item["blocker_code"] = exc.code
            block(entry["id"], exc.code, exc.detail)
            continue
        item["repository"] = identity
        common = session_scope.common_dir(checkout)
        if common is None:
            item["blocker_code"] = "SESSION_TARGET_UNRESOLVED"
            block(entry["id"], item["blocker_code"], "git common directory cannot be resolved")
            continue
        item["git_common_dir"] = str(common)
        if common in seen_common:
            item["blocker_code"] = "WORKSPACE_REPOSITORY_DUPLICATE"
            block(entry["id"], item["blocker_code"], f"same Git repository as entry {seen_common[common]}")
            continue
        if identity.casefold() in seen_identity:
            item["blocker_code"] = "WORKSPACE_REPOSITORY_DUPLICATE"
            block(entry["id"], item["blocker_code"], f"same GitHub identity as entry {seen_identity[identity.casefold()]}")
            continue
        seen_common[common] = entry["id"]
        seen_identity[identity.casefold()] = entry["id"]
        if not _commit_present(checkout, entry["pinned_commit"]):
            item["blocker_code"] = "WORKSPACE_PIN_MISSING"
            block(entry["id"], item["blocker_code"], "pinned commit is not present in the checkout")
            continue
        if access_check is not None:
            item["access"] = access_check(identity)
            if item["access"].get("blocker_code"):
                item["blocker_code"] = item["access"]["blocker_code"]
                block(entry["id"], item["blocker_code"], str(item["access"].get("detail")))
    return resolved, blockers


def effective_readiness(declared: str, observed: str | None) -> str:
    """The further along of the manifest's declared readiness and the observed one."""
    if observed not in READINESS_STATES:
        return declared
    return max(declared, observed, key=READINESS_STATES.index)


def _contract_blockers(by_id: dict[str, dict], deps: dict[str, list[str]], repository_ids: set[str]) -> list[dict]:
    blockers: list[dict] = []
    providers: dict[str, tuple[str, str]] = {}
    for task_id in sorted(by_id):
        for item in by_id[task_id].get("provides") or []:
            if item["contract"] in providers:
                blockers.append({"scope": "task", "id": task_id, "code": "WORKSPACE_CONTRACT_DUPLICATE",
                                 "detail": f"contract {item['contract']} is also provided by {providers[item['contract']][0]}"})
            else:
                providers[item["contract"]] = (task_id, item["version"])
    for task_id in sorted(by_id):
        task = by_id[task_id]
        for item in task.get("requires") or []:
            provider = providers.get(item["contract"])
            if provider is None or provider[0] not in deps[task_id]:
                blockers.append({"scope": "task", "id": task_id, "code": "WORKSPACE_CONTRACT_UNSATISFIED",
                                 "detail": f"contract {item['contract']} is not provided by a direct dependency"})
            elif provider[1] != item["version"]:
                blockers.append({"scope": "task", "id": task_id, "code": "WORKSPACE_CONTRACT_VERSION_MISMATCH",
                                 "detail": f"requires {item['contract']} {item['version']}, {provider[0]} provides {provider[1]}"})
        for check in task.get("checks") or []:
            if check["repository"] not in repository_ids:
                blockers.append({"scope": "task", "id": task_id, "code": "WORKSPACE_CHECK_REPOSITORY_UNKNOWN",
                                 "detail": f"check {check['id']} names undeclared repository {check['repository']}"})
            elif task.get("kind") != "qualification" and check["repository"] != task["repository"]:
                # An implementation task validates only its own produced commit.
                blockers.append({"scope": "task", "id": task_id, "code": "WORKSPACE_CHECK_SCOPE_INVALID",
                                 "detail": f"check {check['id']} must run in the task's own repository"})
        if task.get("kind") == "qualification" and (not task.get("checks") or not deps[task_id]):
            blockers.append({"scope": "task", "id": task_id, "code": "WORKSPACE_QUALIFICATION_INVALID",
                             "detail": "a qualification task needs checks and at least one dependency"})
    return blockers


def plan_tasks(tasks: list[dict], repository_ids: set[str],
               observed: dict[str, dict] | None = None) -> tuple[list[dict], list[list[str]], list[dict]]:
    """Validate the dependency graph and compute deterministic execution waves.

    A task's wave is one more than the highest wave among its dependencies;
    within a wave, tasks are ordered by id. The same manifest always yields the
    same plan. Unknown or self dependencies and cycles are blockers.

    Readiness is declared per task (default ``planned``). ``blocked_by`` lists
    the dependencies whose readiness is neither ``ready_for_downstream`` nor
    ``merged``; a task is ``dispatchable`` when it is still ``planned``, lies on
    no cycle and nothing blocks it.
    """
    blockers: list[dict] = []
    by_id: dict[str, dict] = {}
    for task in tasks:
        if task["id"] in by_id:
            blockers.append({"scope": "task", "id": task["id"], "code": "WORKSPACE_TASK_DUPLICATE",
                             "detail": "task id appears more than once"})
            continue
        by_id[task["id"]] = task
    deps: dict[str, list[str]] = {}
    for task_id, task in by_id.items():
        if task["repository"] not in repository_ids:
            blockers.append({"scope": "task", "id": task_id, "code": "WORKSPACE_TASK_REPOSITORY_UNKNOWN",
                             "detail": f"repository {task['repository']} is not declared"})
        valid: list[str] = []
        for dep in task.get("depends_on") or []:
            if dep == task_id:
                blockers.append({"scope": "task", "id": task_id, "code": "WORKSPACE_DEPENDENCY_SELF",
                                 "detail": "task depends on itself"})
            elif dep not in by_id:
                blockers.append({"scope": "task", "id": task_id, "code": "WORKSPACE_DEPENDENCY_UNKNOWN",
                                 "detail": f"dependency {dep} is not a declared task"})
            else:
                valid.append(dep)
        deps[task_id] = sorted(valid)

    # Kahn's algorithm with longest-path levels; leftover nodes lie on a cycle.
    indegree = {task_id: len(d) for task_id, d in deps.items()}
    dependents: dict[str, list[str]] = {task_id: [] for task_id in deps}
    for task_id, d in deps.items():
        for dep in d:
            dependents[dep].append(task_id)
    level: dict[str, int] = {}
    ready = sorted(task_id for task_id, n in indegree.items() if n == 0)
    while ready:
        current = ready.pop(0)
        level[current] = max((level[dep] for dep in deps[current]), default=-1) + 1
        for nxt in dependents[current]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
                ready.sort()
    cyclic = sorted(task_id for task_id in deps if task_id not in level)
    if cyclic:
        blockers.append({"scope": "task", "id": cyclic[0], "code": "WORKSPACE_DEPENDENCY_CYCLE",
                         "detail": "dependency cycle among: " + ", ".join(cyclic)})

    waves: list[list[str]] = []
    for task_id in sorted(level, key=lambda t: (level[t], t)):
        while len(waves) <= level[task_id]:
            waves.append([])
        waves[level[task_id]].append(task_id)
    blockers.extend(_contract_blockers(by_id, deps, repository_ids))
    observed = observed or {}
    readiness = {task_id: effective_readiness(task.get("readiness") or "planned",
                                              (observed.get(task_id) or {}).get("readiness"))
                 for task_id, task in by_id.items()}
    validated = {task_id: (observed.get(task_id) or {}).get("validated_contracts") or {} for task_id in by_id}

    def contract_ready(task_id: str, dep: str) -> bool:
        """Every contract task_id needs from dep is observed validated at its version (or dep is merged)."""
        if readiness[dep] == "merged":
            return True
        needed = {i["contract"]: i["version"] for i in by_id[task_id].get("requires") or []}
        offered = {i["contract"] for i in by_id[dep].get("provides") or []}
        return all(validated[dep].get(name) == version for name, version in needed.items() if name in offered)

    planned = []
    for task_id in sorted(by_id):
        task = by_id[task_id]
        kind = task.get("kind") or "implementation"
        enough = QUALIFYING_STATES if kind == "qualification" else UNBLOCKING_STATES
        blocked_by = [dep for dep in deps[task_id] if readiness[dep] not in enough or not contract_ready(task_id, dep)]
        planned.append({"id": task_id, "repository": task["repository"], "title": task["title"],
                        "objective": task.get("objective"), "kind": kind,
                        "issue": task.get("issue"), "closes_issue": task.get("closes_issue"),
                        "max_duration_seconds": task.get("max_duration_seconds"),
                        "provides": task.get("provides") or [],
                        "requires": task.get("requires") or [], "checks": task.get("checks") or [],
                        "depends_on": deps[task_id], "blocks": sorted(dependents[task_id]),
                        "wave": level.get(task_id), "readiness": readiness[task_id], "blocked_by": blocked_by,
                        "dispatchable": readiness[task_id] == "planned" and not blocked_by and task_id in level})
    return planned, waves, blockers


def summarize_tasks(tasks: list[dict]) -> dict:
    """Counts per readiness state (every state listed, zero included) and the dispatchable ids, sorted."""
    counts = {state: 0 for state in READINESS_STATES}
    for task in tasks:
        counts[task["readiness"]] += 1
    return {"tasks": len(tasks), "readiness": counts,
            "dispatchable": sorted(t["id"] for t in tasks if t["dispatchable"])}


def plan(manifest_path: Path, *, access_check: AccessCheck | None = gh_access, schema: dict | None = None,
         orchestrator_root: Path = ROOT, observed: dict[str, dict] | None = None) -> dict:
    """``observed`` overlays per-task state recorded by the workspace tools
    (readiness, validated_contracts); the manifest itself is never changed."""
    manifest = load_manifest(manifest_path)
    errors = validate_manifest(manifest, schema)
    if errors:
        raise WorkspaceError("manifest violates schemas/workspace.schema.json: " + "; ".join(errors))
    repositories, repo_blockers = resolve_repositories(manifest["repositories"], base=manifest_path.resolve().parent,
                                                       access_check=access_check)
    tasks, waves, task_blockers = plan_tasks(manifest["tasks"], {r["id"] for r in manifest["repositories"]},
                                             observed)
    base_branches = {r["id"]: r.get("base_branch", "main") for r in manifest["repositories"]}
    for repo in repositories:
        repo["base_branch"] = base_branches[repo["id"]]
    blockers = repo_blockers + task_blockers
    return {"plan_version": PLAN_VERSION, "workspace_id": manifest["id"], "goal": manifest["goal"],
            "status": "BLOCKED" if blockers else "PLANNED", "orchestrator_revision": orchestrator_revision(orchestrator_root),
            "manifest": str(manifest_path.resolve()), "max_parallel": manifest["max_parallel"],
            "access_check": "skipped" if access_check is None else "gh", "repositories": repositories,
            "budget": manifest.get("budget"), "max_duration_seconds": manifest.get("max_duration_seconds"),
            "tasks": tasks, "waves": waves, "summary": summarize_tasks(tasks), "blockers": blockers,
            "mutations": {"git": 0, "github": 0, "workers": 0}}


def _render_task(task: dict) -> str:
    text = f"{task['id']} [{task['repository']}] {task['readiness']}"
    if task["blocked_by"]:
        text += " blocked_by=" + "+".join(task["blocked_by"])
    elif task["dispatchable"]:
        text += " dispatchable"
    return text


def render(result: dict) -> str:
    lines = [f"Workspace {result['workspace_id']}: {result['status']}",
             f"  Orchestrator revision: {result['orchestrator_revision'] or 'unknown'}",
             f"  Access check: {result['access_check']}", "  Repositories:"]
    for repo in result["repositories"]:
        state = repo["blocker_code"] or f"ok ({repo['access']['status']})"
        lines.append(f"    {repo['id']}: {repo['repository']} @ {repo['pinned_commit'][:12]} -> {state}")
    lines.append("  Waves:")
    task_by_id = {t["id"]: t for t in result["tasks"]}
    for index, wave in enumerate(result["waves"], start=1):
        lines.append(f"    wave {index}: " + ", ".join(_render_task(task_by_id[t]) for t in wave))
    summary = result["summary"]
    lines.append("  Readiness: " + ", ".join(f"{state}={summary['readiness'][state]}" for state in READINESS_STATES))
    lines.append("  Dispatchable: " + (", ".join(summary["dispatchable"]) or "none"))
    if result["blockers"]:
        lines.append("  Blockers:")
        for blocker in result["blockers"]:
            lines.append(f"    {blocker['code']} ({blocker['scope']} {blocker['id']}): {blocker['detail']}")
    lines.append("  No worker, worktree, Git or GitHub mutation was performed.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a workspace manifest and print its execution plan (read-only).")
    parser.add_argument("manifest", help="workspace manifest (YAML or JSON)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--no-access-check", action="store_true",
                        help="skip the gh repository access check (reported as skipped in the plan)")
    parser.add_argument("--declared-only", action="store_true",
                        help="ignore observed state recorded by the workspace tools; show manifest readiness only")
    args = parser.parse_args(argv)
    try:
        observed = None
        if not args.declared_only:
            # Same overlay as the scheduler and the Garden, so all three report the same readiness.
            import workspace_run
            raw = load_manifest(Path(args.manifest))
            workspace_id = raw.get("id") if isinstance(raw, dict) and isinstance(raw.get("id"), str) else ""
            try:
                observed = workspace_run.observed_state(workspace_id)
            except workspace_run.WorkspaceRunError as exc:
                raise WorkspaceError(f"workspace record is unreadable: {exc.detail}") from exc
        result = plan(Path(args.manifest), access_check=None if args.no_access_check else gh_access,
                      observed=observed)
    except WorkspaceError as exc:
        if args.json:
            print(json.dumps({"status": "INVALID", "blocker_code": "WORKSPACE_MANIFEST_INVALID", "detail": str(exc)}, indent=2))
        else:
            print(f"WORKSPACE_MANIFEST_INVALID: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else render(result))
    return 0 if result["status"] == "PLANNED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
