#!/usr/bin/env python3
"""Cross-repository contract validation and coordinated draft-PR lifecycle.

A task moves through four observed readiness states, each requiring evidence:

  implemented           its child run ended COMPLETED or PARTIAL (recorded by the launcher)
  validated             `validate`: every check of the task passed on one exact commit,
                        in a temporary detached worktree; its provided contracts are
                        recorded at their declared versions
  ready_for_downstream  `promote`: the task's draft PR exists, its head is still the
                        validated commit, and CI is observed green on it right now
  merged                observed only: `ci` sees the PR merged at the validated commit

A downstream task dispatches only when each dependency is ready_for_downstream
(or merged) and every contract it requires from that dependency is validated at
the required version. Qualification tasks launch no child run: their checks run
across repositories once every dependency is validated, each repository at its
dependency's validated commit (or its pin).

Each PR body references the task's issue with `Refs #N`, or `Closes #N` when the
task sets `closes_issue: true` (or omits it and .pi/autonomy.yaml at the base commit GitHub reports
sets `issue_reference: closes`); `closes_issue: false` always writes `Refs #N`.

Publication goes through github_integration.publish unchanged: .pi/autonomy.yaml
must grant push_branch and create_pr, the head must be a pico/ branch, the plan
is a dry run until --apply with the reviewed --plan-hash, pushes are non-force,
and the PR is a draft. Each repository gets its own PR, which links its
upstream PRs; nothing here merges, marks ready, or pushes to a base branch.

Observed state lives in the workspace record; the manifest is never changed.
Exit codes: 0 ok, 1 blocked or refused, 2 invalid invocation.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import github_integration  # noqa: E402
import github_sync as gh  # noqa: E402
import headless_run  # noqa: E402
import provenance  # noqa: E402
import workspace_run  # noqa: E402

WorkspaceRunError = workspace_run.WorkspaceRunError
RELEASED = {"ready_for_downstream", "merged"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git(checkout: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(checkout), *args], shell=False, capture_output=True, text=True, timeout=timeout)


def _context(manifest: Path, task_id: str) -> tuple[dict, dict, dict]:
    plan = workspace_run._plan(manifest, access_check=None)
    task, repo = workspace_run._task(plan, task_id)
    for blocker in plan["blockers"]:
        if (blocker["scope"] == "task" and blocker["id"] == task_id) or \
           (blocker["scope"] == "repository" and blocker["id"] == repo["id"]):
            raise WorkspaceRunError(blocker["code"], blocker["detail"])
    return plan, task, repo


def _observed(plan: dict, task_id: str) -> dict:
    return workspace_run.observed_state(plan["workspace_id"]).get(task_id) or {}


def _entry(plan: dict, task_id: str) -> dict:
    record = workspace_run.load_workspace(workspace_run.workspace_record_path(plan["workspace_id"])) or {"tasks": {}}
    return record["tasks"].get(task_id) or {}


def _update(plan: dict, manifest: Path, task_id: str, mutate) -> dict:
    with workspace_run.locked_workspace(plan["workspace_id"]):
        path, record = workspace_run._workspace(plan, manifest)
        entry = record["tasks"].setdefault(task_id, {})
        mutate(entry.setdefault("observed", {}))
        workspace_run.save_workspace(path, record)
        return entry


def _commit_of(checkout: Path, ref: str) -> str:
    proc = _git(checkout, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if proc.returncode or not proc.stdout.strip():
        raise WorkspaceRunError("WORKSPACE_HEAD_UNRESOLVED", f"{ref} is not a local commit")
    return proc.stdout.strip()


def run_check(check: dict, checkout: Path, commit: str, scratch: Path) -> dict:
    """Run one check with shell=False in a temporary detached worktree at `commit`."""
    scratch.mkdir(parents=True, exist_ok=True)
    tree = scratch / f"{check['id']}-{commit[:12]}-{uuid.uuid4().hex[:8]}"
    log_path = scratch / f"{tree.name}.log"
    result = {"id": check["id"], "repository": check["repository"], "commit": commit, "status": "error",
              "exit_code": None, "duration_ms": None, "log_path": str(log_path)}
    added = _git(checkout, "worktree", "add", "--detach", str(tree), commit, timeout=120)
    if added.returncode:
        result["status"] = "setup_failed"
        return result
    started = time.monotonic()
    try:
        with open(log_path, "wb") as log:
            proc = subprocess.run(check["argv"], cwd=str(tree), shell=False, stdin=subprocess.DEVNULL,
                                  stdout=log, stderr=subprocess.STDOUT, timeout=check.get("timeout_seconds", 600))
        result["exit_code"] = proc.returncode
        result["status"] = "passed" if proc.returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
    except OSError:
        result["status"] = "not_executable"
    finally:
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        _git(checkout, "worktree", "remove", "--force", str(tree), timeout=120)
        _git(checkout, "worktree", "prune")
    return result


def _scratch(plan: dict, task_id: str) -> Path:
    return headless_run.state_root() / "checks" / plan["workspace_id"] / task_id


def validate(*, manifest: Path, task_id: str, head: str | None = None) -> dict:
    plan, task, repo = _context(manifest, task_id)
    observed = _observed(plan, task_id)
    if observed.get("readiness") in RELEASED:
        raise WorkspaceRunError("WORKSPACE_TASK_ALREADY_RELEASED", f"task is already {observed['readiness']}")
    if not task["checks"]:
        raise WorkspaceRunError("WORKSPACE_CHECKS_REQUIRED", "a task needs at least one check to be validated")
    repos = {r["id"]: r for r in plan["repositories"]}
    targets: dict[str, str] = {}
    if task["kind"] == "qualification":
        if task["blocked_by"]:
            raise WorkspaceRunError("WORKSPACE_TASK_BLOCKED", "dependencies not validated: " + ", ".join(task["blocked_by"]))
        all_observed = workspace_run.observed_state(plan["workspace_id"])
        tasks = {t["id"]: t for t in plan["tasks"]}
        for repo_id in sorted({c["repository"] for c in task["checks"]}):
            commits = {all_observed.get(d, {}).get("validated_commit") for d in task["depends_on"]
                       if tasks[d]["repository"] == repo_id} - {None}
            if len(commits) > 1:
                raise WorkspaceRunError("WORKSPACE_QUALIFICATION_AMBIGUOUS",
                                        f"dependencies in {repo_id} were validated at different commits")
            targets[repo_id] = commits.pop() if commits else repos[repo_id]["pinned_commit"]
    else:
        if (observed.get("readiness") or "planned") not in {"implemented", "validated"}:
            raise WorkspaceRunError("WORKSPACE_TASK_NOT_IMPLEMENTED", "the task's child run has not completed")
        if not head or not head.startswith("pico/"):
            raise WorkspaceRunError("WORKSPACE_HEAD_INVALID", "--head must name the local pico/ branch holding the work")
        checkout = Path(repo["resolved_path"])
        commit = _commit_of(checkout, f"refs/heads/{head}")
        if _git(checkout, "merge-base", "--is-ancestor", repo["pinned_commit"], commit).returncode:
            raise WorkspaceRunError("WORKSPACE_HEAD_UNRELATED", "the head does not descend from the pinned commit")
        targets[repo["id"]] = commit
    results = [run_check(check, Path(repos[check["repository"]]["resolved_path"]), targets[check["repository"]],
                         _scratch(plan, task_id)) for check in task["checks"]]
    passed = all(r["status"] == "passed" for r in results)

    def mutate(obs: dict) -> None:
        obs["checks"] = results
        obs["validation"] = {"status": "passed" if passed else "failed", "at": _now(),
                             "commits": targets, "head_branch": head}
        if passed:
            obs["readiness"] = "validated"
            obs["validated_contracts"] = {c["contract"]: c["version"] for c in task["provides"]}
            if task["kind"] != "qualification":
                obs["validated_commit"] = targets[repo["id"]]
                obs["head_branch"] = head
        else:
            for key in ("validated_contracts", "validated_commit", "head_branch", "pr", "ci"):
                obs.pop(key, None)
            if obs.get("readiness") == "validated":
                obs.pop("readiness")
    entry = _update(plan, manifest, task_id, mutate)
    return {"ok": passed, "task_id": task_id, "status": "VALIDATED" if passed else "CHECK_FAILED",
            "error_code": None if passed else "WORKSPACE_CHECK_FAILED", "checks": results, "observed": entry["observed"]}


def issue_reference_decision(task: dict, repo: dict, adapter=gh) -> dict:
    """Whether the task's PR closes its issue on merge, and what decided it.

    An explicit `closes_issue` on the task wins (`source: task`); without one,
    the task follows its repository's `issue_reference` (default `refs`) at the
    base commit GitHub reports (`source: repository`), the same value the
    publication gate trusts.
    """
    if isinstance(task.get("closes_issue"), bool):
        return {"closes": task["closes_issue"], "source": "task", "base_commit": None, "policy_blob": None}
    try:
        reference, blob, base_commit = github_integration.verified_issue_reference(
            repo["repository"], repo.get("base_branch", "main"), adapter)
    except github_integration.IntegrationError as exc:
        raise WorkspaceRunError(exc.code, exc.detail) from exc
    except (gh.GithubSyncError, ValueError) as exc:
        raise WorkspaceRunError("GITHUB_READ_FAILED", "could not read the base branch's issue policy") from exc
    return {"closes": reference == "closes", "source": "repository", "base_commit": base_commit,
            "policy_blob": blob}


def pr_body(plan: dict, task: dict, repo: dict, observed: dict, decision: dict | None = None) -> str:
    tasks = {t["id"]: t for t in plan["tasks"]}
    repos = {r["id"]: r for r in plan["repositories"]}
    every = workspace_run.observed_state(plan["workspace_id"])
    decision = decision if decision is not None else issue_reference_decision(task, repo)
    keyword = "Closes" if decision["closes"] else "Refs"
    lines = [f"{keyword} #{task['issue']}", "",
             f"Part of workspace `{plan['workspace_id']}`, task `{task['id']}`: {task['title']}", "",
             f"Validated commit: `{observed['validated_commit']}`"]
    for check in observed.get("checks") or []:
        lines.append(f"- check `{check['id']}`: {check['status']}")
    if task["provides"]:
        lines += ["", "Provides: " + ", ".join(f"`{c['contract']}` {c['version']}" for c in task["provides"])]
    if task["requires"]:
        lines += ["", "Requires: " + ", ".join(f"`{c['contract']}` {c['version']}" for c in task["requires"])]
    depends = []
    for dep in task["depends_on"]:
        dep_task, seen = tasks[dep], every.get(dep) or {}
        dep_repo = repos[dep_task["repository"]]["repository"]
        if (seen.get("pr") or {}).get("number"):
            depends.append(f"- {dep_repo}#{seen['pr']['number']} (task `{dep}`)")
        elif dep_task["readiness"] == "merged":
            depends.append(f"- {dep_repo} task `{dep}` (already merged)")
        elif dep_task["kind"] != "qualification":
            raise WorkspaceRunError("WORKSPACE_UPSTREAM_UNPUBLISHED", f"upstream task {dep} has no draft PR yet")
    if depends:
        lines += ["", "Depends on:"] + depends
    lines += ["", "Coordinated by a PICO workspace. Each repository's PR is reviewed and merged on its own; "
              "nothing merges automatically.", ""]
    body = "\n".join(lines)
    if provenance._contains_secret_like_content(body):
        raise WorkspaceRunError("WORKSPACE_BODY_INVALID", "the PR body looks like it contains a secret")
    return body


def publish(*, manifest: Path, task_id: str, apply: bool = False, plan_hash: str | None = None, adapter=gh,
            grant: dict | None = None) -> dict:
    plan, task, repo = _context(manifest, task_id)
    if task["kind"] == "qualification":
        raise WorkspaceRunError("WORKSPACE_TASK_NOT_PUBLISHABLE", "qualification tasks have no pull request")
    observed = _observed(plan, task_id)
    if observed.get("readiness") not in {"validated", "ready_for_downstream"} or not observed.get("validated_commit"):
        raise WorkspaceRunError("WORKSPACE_TASK_NOT_VALIDATED", "validate the task before publishing it")
    if not task.get("issue"):
        raise WorkspaceRunError("WORKSPACE_ISSUE_REQUIRED", "set the task's issue so the PR can reference it")
    checkout = Path(repo["resolved_path"])
    if _commit_of(checkout, f"refs/heads/{observed['head_branch']}") != observed["validated_commit"]:
        raise WorkspaceRunError("WORKSPACE_HEAD_CHANGED", "the head moved since validation; validate again")
    decision = issue_reference_decision(task, repo, adapter)
    body = pr_body(plan, task, repo, observed, decision)
    scratch = _scratch(plan, task_id)
    scratch.mkdir(parents=True, exist_ok=True)
    body_file = scratch / "pr-body.md"
    body_file.write_text(body, encoding="utf-8")
    result = github_integration.publish(worktree=checkout, base=repo["base_branch"], head=observed["head_branch"],
                                        title=task["title"], body_file=body_file, explicit_repo=repo["repository"],
                                        apply=apply, expected_plan_hash=plan_hash, adapter=adapter,
                                        provenance_root=headless_run.ROOT, grant=grant,
                                        closes_issue=task["issue"] if task.get("closes_issue") is True else None)
    if result["status"] == "COMPLETED" and result.get("pr"):
        def mutate(obs: dict) -> None:
            obs["pr"] = {"number": result["pr"], "repository": repo["repository"],
                         "publication_run_id": result["run_id"], "published_at": _now()}
        _update(plan, manifest, task_id, mutate)
    ok = result["status"] in {"DRY_RUN", "COMPLETED"}
    return {"ok": ok, "task_id": task_id, **result, "issue_reference_decision": decision}


def ci(*, manifest: Path, task_id: str, adapter=gh) -> dict:
    plan, task, repo = _context(manifest, task_id)
    observed = _observed(plan, task_id)
    pr = observed.get("pr") or {}
    if not pr.get("number"):
        raise WorkspaceRunError("WORKSPACE_TASK_UNPUBLISHED", "the task has no draft PR yet")
    try:
        data = adapter.pr_checks(pr["number"], repo=repo["repository"])
    except gh.GithubSyncError as exc:
        raise WorkspaceRunError("WORKSPACE_CI_UNAVAILABLE", str(exc)[:300]) from exc
    status = gh.classify_checks(data["statusCheckRollup"])
    if data["headRefOid"] != observed.get("validated_commit"):
        status = "stale"
    state = str(data["state"]).lower()

    def mutate(obs: dict) -> None:
        obs["ci"] = {"status": status, "head": data["headRefOid"], "pr_state": state,
                     "checks": len(data["statusCheckRollup"]), "observed_at": _now()}
        if state == "merged" and status != "stale":
            obs["readiness"] = "merged"
    entry = _update(plan, manifest, task_id, mutate)
    return {"ok": status == "green", "task_id": task_id, "ci": entry["observed"]["ci"],
            "readiness": entry["observed"].get("readiness")}


def promote(*, manifest: Path, task_id: str, adapter=gh) -> dict:
    """Declare a validated, published, CI-green task ready for downstream consumption."""
    observed = ci(manifest=manifest, task_id=task_id, adapter=adapter)
    if observed["readiness"] == "merged":
        return {"ok": True, "task_id": task_id, "readiness": "merged", "ci": observed["ci"]}
    if observed["ci"]["status"] != "green":
        raise WorkspaceRunError("WORKSPACE_CI_NOT_GREEN", f"CI is {observed['ci']['status']} on the validated commit")
    plan, _, _ = _context(manifest, task_id)

    def mutate(obs: dict) -> None:
        obs["readiness"] = "ready_for_downstream"
        obs["promoted_at"] = _now()
    entry = _update(plan, manifest, task_id, mutate)
    return {"ok": True, "task_id": task_id, "readiness": entry["observed"]["readiness"], "ci": observed["ci"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate contracts and coordinate per-repository draft PRs.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "publish", "ci", "promote"):
        p = sub.add_parser(name)
        p.add_argument("manifest", type=Path)
        p.add_argument("--task", required=True)
        if name == "validate":
            p.add_argument("--head", help="local pico/ branch holding the task's work (implementation tasks)")
        if name == "publish":
            p.add_argument("--apply", action="store_true", help="push and open the draft PR")
            p.add_argument("--plan-hash", help="plan_hash from the reviewed dry run")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            result = validate(manifest=args.manifest, task_id=args.task, head=args.head)
        elif args.command == "publish":
            result = publish(manifest=args.manifest, task_id=args.task, apply=args.apply, plan_hash=args.plan_hash)
        elif args.command == "ci":
            result = ci(manifest=args.manifest, task_id=args.task)
        else:
            result = promote(manifest=args.manifest, task_id=args.task)
    except WorkspaceRunError as exc:
        print(json.dumps({"ok": False, "error_code": exc.code, "error": exc.detail}, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
