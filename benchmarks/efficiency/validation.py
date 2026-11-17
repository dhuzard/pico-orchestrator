"""Resolve a completed pilot's integrated head from provenance before validating it."""
import re
import subprocess
from pathlib import Path

SHA = re.compile(r"^[0-9a-f]{40}$")


def integrated_heads(record, run_id, repository):
    if not isinstance(record, dict) or record.get("run_id") != run_id:
        return set()
    parent = record.get("parent_orchestration")
    if (not isinstance(parent, dict) or parent.get("final_status") != "COMPLETED"
            or not isinstance(parent.get("target_repository"), str)
            or parent["target_repository"].casefold() != repository.casefold()):
        return set()
    waves = {w.get("id"): w for w in parent.get("waves", []) if isinstance(w, dict)}
    heads = set()
    for phase in parent.get("phases", []):
        if not isinstance(phase, dict) or phase.get("status") != "completed":
            continue
        for check in phase.get("diff_floor_checks", []):
            if not isinstance(check, dict):
                continue
            wave = waves.get(check.get("wave_id")) or {}
            head = check.get("head")
            if (check.get("operation") == "parent" and check.get("status") == "passed"
                    and wave.get("status") == "integrated" and phase.get("id") in wave.get("task_ids", [])
                    and isinstance(head, str) and SHA.fullmatch(head)):
                heads.add(head)
    return heads


def find_head(state, workspace_id, task_id, repository, checkout, metrics):
    """A single persisted integrated SHA and one local pico branch at that exact SHA, or unknown."""
    try:
        ws = metrics.load_workspace_record(state, workspace_id)
        entry = (ws.get("tasks") or {}).get(task_id) or {}
        run_id = entry.get("run_id")
        if not isinstance(run_id, str):
            return None, "workspace has no linked run"
        record = metrics.load_run(state, run_id)
        heads = integrated_heads(record, run_id, repository)
        if len(heads) != 1:
            return None, "integrated commit evidence is missing or ambiguous"
        commit = next(iter(heads))
        result = subprocess.run(["git", "-C", str(checkout), "for-each-ref", "--format=%(refname:short) %(objectname)",
                                 "refs/heads/pico/"], capture_output=True, text=True, check=True, timeout=30)
        branches = [parts[0] for line in result.stdout.splitlines() if len(parts := line.split()) == 2 and parts[1] == commit]
        if len(branches) != 1:
            return None, "no unique local pico branch points to the integrated commit"
        return {"head": branches[0], "commit": commit, "run_id": run_id}, None
    except (metrics.MetricsError, OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        return None, "workspace, run, or local branch evidence could not be read"


def validate_completed(pico, env, manifest, workspace, schedule, state, real):
    results = []
    repositories = {r["id"]: r for r in workspace["repositories"]}
    for row in (schedule or {}).get("tasks") or []:
        task = next((t for t in workspace["tasks"] if t["id"] == row.get("id")), None)
        if task is None or task.get("kind") == "qualification" or row.get("state") != "completed":
            continue
        repo = repositories[task["repository"]]
        evidence, reason = find_head(Path(state), workspace["id"], task["id"], repo["repository"],
                                     Path(manifest).parent / repo["path"], real.benchmark_metrics)
        if evidence is None:
            results.append({"task": task["id"], "validation": "skipped", "reason": reason})
            continue
        step = real.execute([pico, "workspace", "validate", str(manifest), "--task", task["id"],
                             "--head", evidence["head"]], env, "validate")
        # workspace validate resolves the branch again; demand the result refers to
        # the evidence commit to catch any change between discovery and validation.
        output = step["output"] or {}
        observed = output.get("observed") or {}
        committed = observed.get("validated_commit")
        results.append({"task": task["id"], **evidence, "validated_commit_observed": committed,
                        "validation": output.get("status", "error") if committed == evidence["commit"] else "unverified",
                        "exit_code": step["exit_code"],
                        "checks": [{"id": c.get("id"), "status": c.get("status")} for c in output.get("checks") or []]})
    return results
