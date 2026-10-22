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
task sets `closes_issue: true` (or omits it and .pi/autonomy.yaml on the default branch GitHub reports
sets `issue_reference: closes`); `closes_issue: false` always writes `Refs #N`.

Publication goes through github_integration.publish unchanged: .pi/autonomy.yaml
on the default branch on GitHub must grant push_branch and create_pr, the head must be a pico/ branch, the plan
is a dry run until --apply with the reviewed --plan-hash, pushes are non-force,
and the PR is a draft. Each repository gets its own PR, which links its
upstream PRs; nothing here merges, marks ready, or pushes to a base branch.

`validate` and `publish` also run the post-diff path-floor check on the work
commit against the pinned commit (#76): its changed paths are matched against
the built-in floors and the `.pi/routing.yaml` path floors of the pinned
commit, never the work's own copy, and compared with the routing decisions the
task's child run recorded. A commit those floors put above every recorded
decision is refused with CLASSIFICATION_UNDERRATED_BY_DIFF; one that matches a
floor while no recorded decision can be read is refused with
CLASSIFICATION_DIFF_UNVERIFIABLE.

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
import routing_engine as engine  # noqa: E402
import workspace_run  # noqa: E402

ORCHESTRATOR_ROOT = Path(__file__).resolve().parent.parent

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


def _recorded_routing(run_id: str | None) -> list[dict]:
    """Routing summaries the child run recorded for its phases (and their re-routes); empty when unreadable."""
    if not run_id:
        return []
    try:
        record = provenance.get_run(headless_run.ROOT, run_id)
    except (provenance.ProvenanceError, OSError, ValueError):
        return []
    summaries = []
    for phase in (record.get("parent_orchestration") or {}).get("phases") or []:
        for item in [phase, *(phase.get("escalations") or [])]:
            if isinstance(item, dict) and isinstance(item.get("routing"), dict):
                summaries.append(item["routing"])
    return summaries


def path_floor_check(plan: dict, task: dict, repo: dict, commit: str) -> dict:
    """routing_engine.check_recorded_path_floors for the task's work commit against its pinned commit."""
    checkout = Path(repo["resolved_path"])
    base = repo["pinned_commit"]
    unverifiable = {"ok": False, "blocked": True, "blocker_code": engine.CLASSIFICATION_DIFF_UNVERIFIABLE,
                    "diff_path_floor_matches": []}
    diff = subprocess.run(["git", "-C", str(checkout), "diff", "--name-only", "--no-renames", "-z", base, commit, "--"],
                          shell=False, capture_output=True, timeout=60)
    if diff.returncode:
        return {**unverifiable, "reason": "the diff from the pinned commit could not be read"}
    try:
        paths = [name for name in diff.stdout.decode("utf-8").split("\0") if name]
    except UnicodeDecodeError:
        return {**unverifiable, "reason": "a changed path is not valid UTF-8"}
    assets = engine.load_policy_assets(ORCHESTRATOR_ROOT)
    project_routing = None
    if _git(checkout, "cat-file", "-e", f"{base}:.pi/routing.yaml").returncode == 0:
        shown = _git(checkout, "show", f"{base}:.pi/routing.yaml")
        try:
            project_routing = (engine.yaml.load(shown.stdout, Loader=engine.UniqueKeySafeLoader)
                               if not shown.returncode else None)
        except engine.yaml.YAMLError:
            shown = None
        if (shown is None or shown.returncode or (project_routing is not None and
                engine.validate_project_routing_instance(project_routing, assets.project_schema))):
            return {**unverifiable, "reason": "the pinned commit's .pi/routing.yaml cannot be read or is invalid"}
    run_id = (_entry(plan, task["id"]) or {}).get("run_id")
    verdict = engine.check_recorded_path_floors(paths, project_routing, _recorded_routing(run_id), assets.task_schema)
    return {**verdict, "base": base, "commit": commit}


def _refuse_underrated(verdict: dict) -> None:
    if verdict["blocked"]:
        floored = sorted({path for match in verdict.get("diff_path_floor_matches") or [] for path in match["matched_paths"]})
        raise WorkspaceRunError(verdict["blocker_code"],
                                f"{verdict.get('reason', 'path floors')}: {', '.join(floored[:12]) or 'no floored path'}")


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
        _refuse_underrated(path_floor_check(plan, task, repo, commit))
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
    the task follows its repository's `issue_reference` (default `refs`) on the
    default branch GitHub reports (`source: repository`), the same value the
    publication gate trusts.
    """
    if isinstance(task.get("closes_issue"), bool):
        return {"closes": task["closes_issue"], "source": "task", "policy_branch": None, "policy_commit": None,
                "policy_blob": None}
    try:
        reference, blob, commit, branch = github_integration.verified_issue_reference(repo["repository"], adapter)
    except github_integration.IntegrationError as exc:
        raise WorkspaceRunError(exc.code, exc.detail) from exc
    except (gh.GithubSyncError, ValueError) as exc:
        raise WorkspaceRunError("GITHUB_READ_FAILED", "could not read the default branch's issue policy") from exc
    return {"closes": reference == "closes", "source": "repository", "policy_branch": branch,
            "policy_commit": commit, "policy_blob": blob}


def pr_body(plan: dict, task: dict, repo: dict, observed: dict, decision: dict | None = None,
            supersedes: list[int] | None = None) -> str:
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
    if supersedes:
        lines += ["", "Supersedes " + ", ".join(f"#{n}" for n in supersedes) + " (closed without merging)."]
    lines += ["", "Coordinated by a PICO workspace. Each repository's PR is reviewed and merged on its own; "
              "nothing merges automatically.", ""]
    body = "\n".join(lines)
    if provenance._contains_secret_like_content(body):
        raise WorkspaceRunError("WORKSPACE_BODY_INVALID", "the PR body looks like it contains a secret")
    return body


def publish(*, manifest: Path, task_id: str, apply: bool = False, plan_hash: str | None = None, adapter=gh,
            new_branch: bool = False) -> dict:
    """Dry-run or apply the task's draft PR. `new_branch` republishes it after its PR was closed without merging
    (#67): the validated commit goes to the next free `<head_branch>-<n>` branch, and the PR names the closed PRs
    it supersedes. The closed PR's branch is never reused, reopened or overwritten, and a branch holding another
    task's head or anyone else's PR is skipped. Pass `new_branch` to both the dry run and the apply. A released
    task goes back to validated: its CI and promotion were observed on the superseded PR."""
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
    # Re-checked here: the floors of the pinned commit and the run's record are read again, not taken from validation.
    _refuse_underrated(path_floor_check(plan, task, repo, observed["validated_commit"]))
    decision = issue_reference_decision(task, repo, adapter)
    republished = None
    republication = {}
    if new_branch:
        every = workspace_run.observed_state(plan["workspace_id"])
        same_repo = {t["id"] for t in plan["tasks"] if t["repository"] == task["repository"] and t["id"] != task_id}
        reserved = frozenset(branch for other in same_repo for branch in
                             ((every.get(other) or {}).get("head_branch"),
                              ((every.get(other) or {}).get("pr") or {}).get("branch")) if branch)
        republication = {"recorded_pr": observed.get("pr"), "reserved": reserved}
        try:
            republished = github_integration.republication_branch(checkout, repo["repository"], repo["base_branch"],
                                                                  observed["head_branch"], adapter, **republication)
        except github_integration.IntegrationError as exc:
            raise WorkspaceRunError(exc.code, exc.detail) from exc
        except (gh.GithubSyncError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceRunError("PUBLICATION_FAILED",
                                    f"could not inspect the republication branches: {type(exc).__name__}") from exc
    body = pr_body(plan, task, repo, observed, decision, supersedes=(republished or {}).get("supersedes"))
    scratch = _scratch(plan, task_id)
    scratch.mkdir(parents=True, exist_ok=True)
    body_file = scratch / "pr-body.md"
    body_file.write_text(body, encoding="utf-8")
    result = github_integration.publish(worktree=checkout, base=repo["base_branch"], head=observed["head_branch"],
                                        title=task["title"], body_file=body_file, explicit_repo=repo["repository"],
                                        apply=apply, expected_plan_hash=plan_hash, adapter=adapter,
                                        provenance_root=headless_run.ROOT,
                                        closes_issue=task["issue"] if task.get("closes_issue") is True else None,
                                        remote_head=(republished or {}).get("remote_head"), **republication)
    if result["status"] == "COMPLETED" and result.get("pr"):
        def mutate(obs: dict) -> None:
            previous = (obs.get("pr") or {}).get("number")
            if previous == result["pr"] and not result["mutations_applied"]:
                return  # a rerun that changed nothing keeps the run that published the PR, and its promotion
            obs["pr"] = {"number": result["pr"], "repository": repo["repository"],
                         "publication_run_id": result["run_id"], "published_at": _now()}
            if republished:
                obs["pr"].update({"branch": republished["remote_head"], "supersedes": republished["supersedes"]})
            if republished and previous != result["pr"]:
                # CI and promotion were observed on the superseded PR: promote again on the new one.
                obs.pop("ci", None)
                obs.pop("promoted_at", None)
                if obs.get("readiness") == "ready_for_downstream":
                    obs["readiness"] = "validated"
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
            p.add_argument("--new-branch", action="store_true",
                           help="after the task's PR was closed unmerged, publish to the next free <head>-<n> branch "
                                "(pass it to both the dry run and --apply)")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            result = validate(manifest=args.manifest, task_id=args.task, head=args.head)
        elif args.command == "publish":
            result = publish(manifest=args.manifest, task_id=args.task, apply=args.apply, plan_hash=args.plan_hash,
                             new_branch=args.new_branch)
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
