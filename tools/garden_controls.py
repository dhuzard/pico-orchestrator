#!/usr/bin/env python3
"""Opt-in actions and read-only control panels for the workspace Garden.

`pico watch --workspace <manifest> --controls` lets the Garden trigger the
existing workspace contract commands (validate, publish, ci, promote) instead
of reimplementing their gates. The viewer stays read-only without the flag.

Security model, all fail closed:
- every request must carry Host 127.0.0.1:<port> or localhost:<port>
  (DNS-rebinding guard; applied to reads too);
- an action is a same-origin JSON POST carrying the per-session token, which
  is generated at launch and printed only in the launch URL's fragment;
- every mutating action is two-step: the first call returns a preview and a
  confirmation bound by HMAC to the exact state previewed (head commit,
  publication plan hash, CI head and status); the second call must return it
  unchanged and the state is recomputed, so anything that moved is refused;
- actions run one at a time, and each is appended to the workspace record's
  `garden_actions` log. Publication additionally creates its own provenance
  run through github_integration.publish, as on the command line.

Nothing here merges, marks a PR ready or deploys. Policy edits are limited
to the permissions in EDITABLE_PERMISSIONS (set_permission) and to the
bounded approval of local mutation (approve_mutation: create_worktrees and
commit only), each previewed as an exact diff; neither commits the change,
and commit_policy commits only that file on the base branch, never pushed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atomic_io  # noqa: E402
import autonomy_policy  # noqa: E402
import difflib  # noqa: E402
import io  # noqa: E402
import os  # noqa: E402
from contextlib import redirect_stdout  # noqa: E402
import model_profiles  # noqa: E402
import routing_engine as engine  # noqa: E402
import workspace_authorization  # noqa: E402
import workspace_contracts  # noqa: E402
import workspace_run  # noqa: E402
import workspace_scheduler  # noqa: E402
import workspace_action_view  # noqa: E402

TASK_ACTIONS = ("validate", "publish", "ci", "promote", "cancel")
WORKSPACE_ACTIONS = ("schedule", "set_permission", "approve_mutation", "commit_policy", "set_model", "reset_model")
ACTIONS = TASK_ACTIONS + WORKSPACE_ACTIONS
# Editable from the Garden; mark_pr_ready, merge and deploy stay file-only.
EDITABLE_PERMISSIONS = ("create_backlog_items", "create_github_issues", "create_worktrees", "commit",
                        "push_branch", "create_pr", "comment_on_issue", "comment_on_pr")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
BRANCH_RE = re.compile(r"^pico/[A-Za-z0-9._/-]{1,200}$")
MAX_BODY = 16 * 1024
MAX_AUDIT = 100
PERMISSIONS = ("create_backlog_items", "create_github_issues", "create_worktrees", "commit", "push_branch",
               "create_pr", "comment_on_issue", "comment_on_pr", "mark_pr_ready", "deploy")


class ControlError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        self.status, self.code, self.detail = status, code, detail
        super().__init__(f"{code}: {detail}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_policy_location(path: Path) -> Path:
    """Fail closed unless `.pi/autonomy.yaml` is a plain path inside its checkout.

    A committed symlink at `.pi` or `.pi/autonomy.yaml` would otherwise let a
    confirmed permission change read or overwrite a file outside the checkout.
    Returns the checkout root the policy belongs to.
    """
    top = path.parent.parent
    unsafe = ControlError(409, "AUTONOMY_POLICY_PATH_UNSAFE", "the policy path is a symlink or leaves the checkout")
    try:
        atomic_io.check_no_symlinks(path, top)
    except atomic_io.UnsafePathError as exc:
        raise unsafe from exc
    resolved_top = top.resolve()
    if path.parent.name != ".pi" or path.resolve().parent != resolved_top / ".pi":
        raise unsafe
    return top


def host_allowed(host: str | None, port: int) -> bool:
    return host in {f"127.0.0.1:{port}", f"localhost:{port}"}


class Controls:
    def __init__(self, manifest: Path, port: int, *, adapter=None, token: str | None = None):
        self.manifest = manifest
        self.port = port
        self.token = token or secrets.token_urlsafe(32)
        self._secret = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self.adapter = adapter if adapter is not None else workspace_contracts.gh
        self.pi_executable = os.environ.get("PI_EXECUTABLE", "pi")
        self._scheduler: threading.Thread | None = None
        self.last_schedule: dict | None = None

    # -- request checks ----------------------------------------------------
    def check_token(self, headers) -> None:
        """Constant-time check of the session token sent as X-Pico-Token."""
        supplied = headers.get("X-Pico-Token") or ""
        if not hmac.compare_digest(supplied.encode(), self.token.encode()):
            raise ControlError(403, "CONTROL_TOKEN_INVALID", "open the Garden from the URL printed at launch")

    def authorize(self, headers, body: bytes) -> dict:
        origin = headers.get("Origin")
        if origin not in {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}:
            raise ControlError(403, "CONTROL_ORIGIN_REJECTED", "actions must come from this Garden page")
        self.check_token(headers)
        if (headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            raise ControlError(415, "CONTROL_BODY_INVALID", "actions take a JSON body")
        if len(body) > MAX_BODY:
            raise ControlError(413, "CONTROL_BODY_INVALID", "request body is too large")
        try:
            data = json.loads(body or b"{}")
        except ValueError as exc:
            raise ControlError(400, "CONTROL_BODY_INVALID", "request body is not JSON") from exc
        if not isinstance(data, dict):
            raise ControlError(400, "CONTROL_BODY_INVALID", "request body must be an object")
        return data

    # -- read-only panels --------------------------------------------------
    def _plan(self) -> dict:
        return workspace_run._plan(self.manifest, access_check=None)

    def panels(self) -> dict:
        plan = self._plan()
        repositories = []
        for repo in plan["repositories"]:
            item = {"id": repo["id"], "repository": repo["repository"], "base_branch": repo.get("base_branch", "main"),
                    "source": None, "profile": None, "pr_granularity": None, "issue_reference": None, "permissions": {}, "merge_enabled": None,
                    "limits": {}, "error_code": None}
            checkout = Path(repo["resolved_path"]) if repo.get("resolved_path") else None
            path = autonomy_policy.policy_path(checkout) if checkout else None
            if path is None:
                item["error_code"] = repo.get("blocker_code") or "WORKSPACE_REPOSITORY_PATH_MISSING"
            elif not path.is_file():
                item["source"] = "default (no .pi/autonomy.yaml)"
                item["issue_reference"] = autonomy_policy.DEFAULTS["issue_reference"]
                item["permissions"] = {name: False for name in PERMISSIONS}
                item["merge_enabled"] = False
                item["limits"] = {k: v for k, v in autonomy_policy.DEFAULTS.items() if isinstance(v, int)}
            else:
                try:
                    policy = yaml.safe_load(path.read_text(encoding="utf-8"))
                    if autonomy_policy.validate(policy):
                        raise ValueError
                except (OSError, ValueError, yaml.YAMLError):
                    item["error_code"] = "AUTONOMY_POLICY_INVALID"
                else:
                    permissions = policy.get("permissions") or {}
                    item.update({"source": ".pi/autonomy.yaml", "profile": policy.get("profile"),
                                 "pr_granularity": policy.get("pr_granularity", "batch"),
                                 "issue_reference": policy.get("issue_reference",
                                                               autonomy_policy.DEFAULTS["issue_reference"]),
                                 "permissions": {name: permissions.get(name) is True for name in PERMISSIONS},
                                 "merge_enabled": (permissions.get("merge") or {}).get("enabled") is True,
                                 "limits": {k: v for k, v in (policy.get("limits") or {}).items() if isinstance(v, int)}})
            if checkout is not None and path is not None:
                status = subprocess.run(["git", "-C", str(checkout), "status", "--porcelain", "--",
                                         str(path.relative_to(checkout))], shell=False, capture_output=True,
                                        text=True, timeout=10)
                item["policy_uncommitted"] = bool(status.stdout.strip())
            repositories.append(item)
        try:
            path = model_profiles.default_config_path()
            config = model_profiles._load_raw(path) if path.exists() else None
            summary = model_profiles._summary(path, config)
            models = {"configured": bool(summary.get("configured")), "models": summary.get("models", {}),
                      "ambiguity_capacities": summary.get("ambiguity_capacities", {}),
                      "choices": self._model_choices(summary), "floors": dict(engine.TIER_AMBIGUITY_FLOORS),
                      "error_code": None}
        except Exception:  # model config problems are reported, never served in detail
            models = {"configured": None, "models": {}, "error_code": "MODEL_CONFIG_INVALID"}
        running = self._scheduler is not None and self._scheduler.is_alive()
        record = workspace_run.load_workspace(workspace_run.workspace_record_path(plan["workspace_id"])) or {"tasks": {}}
        entries = workspace_run.status(manifest=self.manifest, access_check=None)["tasks"]
        budget = workspace_scheduler.budget_state(plan, record)
        action_view = workspace_action_view.snapshot(plan, entries, budget=budget, scheduler_running=running)
        return {"repositories": repositories, "models": models,
                "editable_permissions": list(EDITABLE_PERMISSIONS),
                "scheduler": {"running": running, "last": self.last_schedule},
                "action_view": action_view}

    @staticmethod
    def _model_choices(summary: dict) -> dict:
        """Models already bound somewhere in each family, with the highest capacity they carry."""
        base = model_profiles._base_policy()["model_profiles"]
        choices: dict[str, dict] = {}
        for family, tiers in summary.get("models", {}).items():
            pool: dict[str, int] = {}
            entries = [(tier, model, summary["ambiguity_capacities"][family][tier]) for tier, model in tiers.items()]
            entries += [(tier, entry["model"], entry["ambiguity_capacity"])
                        for tier, entry in base.get(family, {}).get("capacity_models", {}).items()]
            for _, model, capacity in entries:
                pool[model] = max(pool.get(model, 0), capacity)
            choices[family] = [{"model": model, "capacity": capacity} for model, capacity in sorted(pool.items())]
        return choices

    def branches(self, task_id: str) -> list[dict]:
        plan, task, repo = self._task(task_id)
        checkout = Path(repo["resolved_path"])
        proc = subprocess.run(["git", "-C", str(checkout), "for-each-ref", "--format=%(refname:short) %(objectname)",
                               "refs/heads/pico/"], shell=False, capture_output=True, text=True, timeout=10)
        branches = []
        for line in proc.stdout.splitlines():
            name, _, commit = line.partition(" ")
            if not BRANCH_RE.fullmatch(name):
                continue
            descends = subprocess.run(["git", "-C", str(checkout), "merge-base", "--is-ancestor",
                                       repo["pinned_commit"], commit], shell=False, capture_output=True,
                                      timeout=10).returncode == 0
            branches.append({"name": name, "commit": commit, "descends_from_pin": descends})
        return sorted(branches, key=lambda b: b["name"])

    # -- actions -----------------------------------------------------------
    def _task(self, task_id) -> tuple[dict, dict, dict]:
        if not isinstance(task_id, str) or not TASK_RE.fullmatch(task_id):
            raise ControlError(400, "CONTROL_TASK_INVALID", "task id is invalid")
        plan = self._plan()
        try:
            task, repo = workspace_run._task(plan, task_id)
        except workspace_run.WorkspaceRunError as exc:
            raise ControlError(404, exc.code, exc.detail) from exc
        if not repo.get("resolved_path"):
            raise ControlError(409, repo.get("blocker_code") or "WORKSPACE_REPOSITORY_PATH_MISSING",
                               "the task's repository checkout cannot be resolved")
        return plan, task, repo

    def _confirm(self, *parts: object) -> str:
        message = json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    def _audit(self, plan: dict, entry: dict) -> None:
        with workspace_run.locked_workspace(plan["workspace_id"]):
            path, record = workspace_run._workspace(plan, self.manifest)
            log = record.setdefault("garden_actions", [])
            log.append({"at": _now(), **entry})
            del log[:-MAX_AUDIT]
            workspace_run.save_workspace(path, record)

    def act(self, data: dict) -> dict:
        action = data.get("action")
        if action not in ACTIONS:
            raise ControlError(400, "CONTROL_ACTION_UNKNOWN", "unknown action")
        if not self._lock.acquire(blocking=False):
            raise ControlError(409, "CONTROL_BUSY", "another action is running; try again when it finishes")
        try:
            confirm = data.get("confirm")
            if confirm is not None and not isinstance(confirm, str):
                raise ControlError(400, "CONTROL_BODY_INVALID", "confirm must be a string")
            if action in TASK_ACTIONS:
                plan, task, repo = self._task(data.get("task"))
                args = (plan, task, repo, data, confirm)
                subject = task["id"]
            else:
                plan, task, repo = self._plan(), None, None
                args = (plan, data, confirm)
                subject = None
            handler = getattr(self, f"_{action}")
            try:
                result = handler(*args)
            except workspace_run.WorkspaceRunError as exc:
                result = {"ok": False, "stage": "refused", "error_code": exc.code, "error": exc.detail}
            except ControlError as exc:
                result = {"ok": False, "stage": "refused", "error_code": exc.code, "error": exc.detail}
            if result.get("stage") != "preview":
                entry = {"action": action, "task": subject, "stage": result.get("stage"),
                         "ok": bool(result.get("ok")), "error_code": result.get("error_code")}
                entry.update(result.pop("audit", {}))
                self._audit(plan, entry)
            result.pop("audit", None)
            return {"action": action, "task": subject, **result}
        finally:
            self._lock.release()

    def _check_confirm(self, confirm: str | None, expected: str) -> None:
        if not hmac.compare_digest(confirm or "", expected):
            raise ControlError(409, "CONTROL_PREVIEW_STALE", "the state changed since the preview; review it again")

    def _validate(self, plan, task, repo, data, confirm):
        head = data.get("head")
        args = {"manifest": self.manifest, "task_id": task["id"]}
        if task["kind"] == "qualification":
            state = {"head": None, "commits": None}
        else:
            if not isinstance(head, str) or not BRANCH_RE.fullmatch(head):
                raise ControlError(400, "WORKSPACE_HEAD_INVALID", "choose the local pico/ branch holding the work")
            match = next((b for b in self.branches(task["id"]) if b["name"] == head), None)
            if match is None:
                raise ControlError(404, "WORKSPACE_HEAD_UNRESOLVED", f"{head} is not a local branch of {repo['repository']}")
            state = {"head": head, "commit": match["commit"]}
            args["head"] = head
        expected = self._confirm("validate", task["id"], state, task["checks"])
        if confirm is None:
            return {"ok": True, "stage": "preview", "confirm": expected,
                    "preview": {"repository": repo["repository"], **state,
                                "checks": [{"id": c["id"], "repository": c["repository"], "argv": c["argv"]}
                                           for c in task["checks"]],
                                "note": "Checks run locally in temporary detached worktrees; nothing is pushed."}}
        self._check_confirm(confirm, expected)
        result = workspace_contracts.validate(**args)
        return {"ok": result["ok"], "stage": "done", "status": result["status"], "error_code": result["error_code"],
                "checks": [{k: c[k] for k in ("id", "repository", "status", "exit_code", "duration_ms")}
                           for c in result["checks"]]}

    def _publish(self, plan, task, repo, data, confirm):
        if "grant_once" in data:
            # Removed (#59): whoever holds the session token, an agent included, could confirm a grant.
            raise ControlError(400, "CONTROL_BODY_INVALID", "one-shot publication grants were removed; allow "
                               "push_branch and create_pr in .pi/autonomy.yaml on the default branch on GitHub")
        preview = workspace_contracts.publish(manifest=self.manifest, task_id=task["id"], adapter=self.adapter)
        if preview["status"] != "DRY_RUN":
            result = {"ok": False, "stage": "refused", "error_code": preview["blocker_code"],
                      "error": "publication was refused by its preconditions or the autonomy policy"}
            if preview["blocker_code"] == "PUBLICATION_NOT_AUTHORIZED":
                result["error"] = ("this repository's .pi/autonomy.yaml on the default branch on GitHub does not allow "
                                   "push_branch and create_pr; allow them in the permissions panel, commit the policy "
                                   "and merge it into the default branch")
            elif preview["blocker_code"] == "AUTONOMY_POLICY_PATH_UNSAFE":
                result["error"] = ("the checkout's .pi/autonomy.yaml is a symlink; "
                                   "restore it before publishing")
            return result
        expected = self._confirm("publish", task["id"], preview["plan_hash"])
        if confirm is None:
            body_path = workspace_contracts._scratch(plan, task["id"]) / "pr-body.md"
            body = body_path.read_text(encoding="utf-8") if body_path.is_file() else ""
            return {"ok": True, "stage": "preview", "confirm": expected,
                    "preview": {**{k: preview["plan"][k] for k in ("repository", "base", "head", "commit", "remote_commit",
                                                                  "existing_pr", "title", "draft")},
                                "body": body, "mutations_planned": int(preview["plan"]["remote_commit"] != preview["plan"]["commit"])
                                + int(preview["plan"]["existing_pr"] is None),
                                "authorization": preview["plan"]["authorization"]}}
        self._check_confirm(confirm, expected)
        result = workspace_contracts.publish(manifest=self.manifest, task_id=task["id"], apply=True,
                                             plan_hash=preview["plan_hash"], adapter=self.adapter)
        return {"ok": result["ok"] and result["status"] == "COMPLETED", "stage": "done", "status": result["status"],
                "audit": {"authorization": preview["plan"]["authorization"]},
                "error_code": result["blocker_code"], "pr": result.get("pr"),
                "url": f"https://github.com/{repo['repository']}/pull/{result['pr']}" if result.get("pr") else None,
                "mutations_applied": result["mutations_applied"]}

    def _ci(self, plan, task, repo, data, confirm):
        # Read-only on GitHub; records the observation, so no confirmation step.
        result = workspace_contracts.ci(manifest=self.manifest, task_id=task["id"], adapter=self.adapter)
        return {"ok": result["ok"], "stage": "done", "ci": result["ci"], "readiness": result["readiness"],
                "error_code": None if result["ok"] else "WORKSPACE_CI_NOT_GREEN"}

    def _promote(self, plan, task, repo, data, confirm):
        observed = workspace_contracts.ci(manifest=self.manifest, task_id=task["id"], adapter=self.adapter)
        ci = observed["ci"]
        expected = self._confirm("promote", task["id"], ci["head"], ci["status"], ci["pr_state"])
        if confirm is None:
            return {"ok": ci["status"] == "green", "stage": "preview", "confirm": expected,
                    "preview": {"repository": repo["repository"], "ci": ci, "readiness": observed["readiness"],
                                "note": "Promotion declares the interface ready for downstream tasks. It does not merge."}}
        self._check_confirm(confirm, expected)
        result = workspace_contracts.promote(manifest=self.manifest, task_id=task["id"], adapter=self.adapter)
        return {"ok": result["ok"], "stage": "done", "readiness": result["readiness"], "ci": result["ci"],
                "error_code": None}

    # -- slice 2: cancel, schedule, permissions, models --------------------
    def _cancel(self, plan, task, repo, data, confirm):
        entry = (workspace_run.load_workspace(workspace_run.workspace_record_path(plan["workspace_id"])) or
                 {"tasks": {}})["tasks"].get(task["id"]) or {}
        if entry.get("execution_status") not in {"launched", "interrupted", "launching"}:
            raise ControlError(409, "CONTROL_NOTHING_TO_CANCEL", "the task has no running or interrupted child run")
        expected = self._confirm("cancel", task["id"], entry.get("execution_id"), entry.get("run_id"),
                                 entry.get("execution_status"))
        if confirm is None:
            return {"ok": True, "stage": "preview", "confirm": expected,
                    "preview": {"repository": repo["repository"], "execution_status": entry.get("execution_status"),
                                "run_id": entry.get("run_id"),
                                "note": "Stops the child process and closes its run as BLOCKED / EXECUTION_CANCELLED."}}
        self._check_confirm(confirm, expected)
        result = workspace_run.cancel(manifest=self.manifest, task_id=task["id"], access_check=None)
        return {"ok": result["task"]["execution_status"] == "cancelled", "stage": "done",
                "status": result["task"]["execution_status"], "error_code": result["task"].get("run_blocker_code")}

    def _schedule(self, plan, data, confirm):
        if self._scheduler is not None and self._scheduler.is_alive():
            raise ControlError(409, "CONTROL_BUSY", "a scheduler started from this Garden is still running")
        allow_mutation = data.get("allow_mutation") is True
        resume = data.get("resume_interrupted") is True
        observed_plan = workspace_run._plan(self.manifest, access_check=None)
        record = workspace_run.load_workspace(workspace_run.workspace_record_path(plan["workspace_id"])) or {"tasks": {}}
        states = {t["id"]: workspace_scheduler.state_of(t, record["tasks"].get(t["id"]), {}) for t in observed_plan["tasks"]}
        # The scheduler's own preflight: tasks without mutation authorization are held, authorized retries launch.
        verdicts = workspace_run.authorization_verdicts(observed_plan, allow_mutation=allow_mutation)
        candidates = [t["id"] for t in observed_plan["tasks"] if t["dispatchable"] and (
            states[t["id"]] == "not_launched" or (states[t["id"]] == workspace_scheduler.AWAITING and workspace_run.retry_due(
                record["tasks"].get(t["id"]), verdicts[t["id"]])))]
        awaiting = sorted(tid for tid in candidates if verdicts[tid]["status"] == "awaiting")
        dispatch = sorted(tid for tid in candidates if tid not in awaiting)
        interrupted = sorted(t for t, state in states.items() if state == "needs_reconciliation")
        mutating = {}
        for repo in observed_plan["repositories"]:
            try:
                granted = workspace_run.headless_run.mutation_permitted(Path(repo["resolved_path"])) \
                    if repo.get("resolved_path") else []
            except workspace_run.headless_run.HeadlessError:
                granted = ["unreadable policy"]
            mutating[repo["id"]] = granted
        running, running_problems = workspace_run.running_tasks(observed_plan, workspace=record)
        budget = workspace_scheduler.budget_state(observed_plan, record, running=running,
                                                  extra_problems=running_problems)
        expected = self._confirm("schedule", dispatch, interrupted if resume else [], allow_mutation,
                                 observed_plan["max_parallel"], mutating, bool(budget and budget["exhausted"]), awaiting)
        if confirm is None:
            launchable = bool(dispatch) and not (budget and (budget["exhausted"] or budget["incomplete"]))
            return {"ok": launchable or bool(resume and interrupted), "stage": "preview", "confirm": expected,
                    "preview": {"dispatch": dispatch, "resume": interrupted if resume else [], "budget": budget,
                                "awaiting_authorization": awaiting,
                                "interrupted": interrupted, "cap": observed_plan["max_parallel"],
                                "allow_mutation": allow_mutation, "mutating_permissions": mutating,
                                "note": "Launches real Pi child runs, one per task, in their own repositories."
                                        + ("" if allow_mutation else " Repositories whose policy grants mutating "
                                           "permissions refuse to launch without allow mutation.")}}
        self._check_confirm(confirm, expected)
        self.last_schedule = {"status": "running", "started_at": _now(), "finished_at": None,
                              "dispatch": dispatch, "error_code": None, "result_status": None}

        def run() -> None:
            try:
                result = workspace_scheduler.run(self.manifest, allow_mutation=allow_mutation,
                                                 resume_interrupted=resume, access_check=None,
                                                 pi_executable=self.pi_executable)
                self.last_schedule.update({"status": "finished", "result_status": result["status"]})
            except workspace_run.WorkspaceRunError as exc:
                self.last_schedule.update({"status": "failed", "error_code": exc.code})
            except Exception:  # never let a scheduler crash take the viewer down
                self.last_schedule.update({"status": "failed", "error_code": "SCHEDULER_FAILED"})
            finally:
                self.last_schedule["finished_at"] = _now()
        self._scheduler = threading.Thread(target=run, name="garden-scheduler", daemon=True)
        self._scheduler.start()
        return {"ok": True, "stage": "started", "status": "SCHEDULER_STARTED",
                "audit": {"dispatch": dispatch, "allow_mutation": allow_mutation, "resume": resume}}

    def _repo(self, plan, repo_id) -> tuple[dict, Path]:
        repo = next((r for r in plan["repositories"] if r["id"] == repo_id), None)
        if repo is None or not repo.get("resolved_path"):
            raise ControlError(404, "CONTROL_REPOSITORY_INVALID", "unknown or unresolved repository")
        return repo, Path(repo["resolved_path"])

    def _set_permission(self, plan, data, confirm):
        repo, checkout = self._repo(plan, data.get("repository"))
        name, value = data.get("permission"), data.get("value")
        if name not in EDITABLE_PERMISSIONS:
            raise ControlError(400, "CONTROL_PERMISSION_LOCKED", "this permission can only be changed in the file")
        if not isinstance(value, bool):
            raise ControlError(400, "CONTROL_BODY_INVALID", "value must be true or false")
        path = autonomy_policy.policy_path(checkout)
        _safe_policy_location(path)
        root = Path(__file__).resolve().parent.parent
        before = path.read_text(encoding="utf-8") if path.is_file() else ""
        source = before or (root / "templates" / "autonomy.yaml").read_text(encoding="utf-8")
        pattern = re.compile(rf"^(\s+{name}:\s*)(true|false)(\s*(#.*)?)$", re.M)
        if len(pattern.findall(source)) != 1:
            raise ControlError(409, "AUTONOMY_POLICY_UNEDITABLE", f"{name} is not a single plain line in the policy")
        after = pattern.sub(lambda m: f"{m.group(1)}{'true' if value else 'false'}{m.group(3)}", source)
        try:
            issues = autonomy_policy.validate(yaml.safe_load(after))
        except yaml.YAMLError:
            issues = ["unparseable"]
        if issues:
            raise ControlError(409, "AUTONOMY_POLICY_INVALID", "the edited policy would not validate")
        diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                            fromfile=".pi/autonomy.yaml", tofile=".pi/autonomy.yaml"))
        expected = self._confirm("set_permission", repo["id"], name, value, hashlib.sha256(before.encode()).hexdigest())
        if confirm is None:
            return {"ok": before != after, "stage": "preview", "confirm": expected,
                    "preview": {"repository": repo["repository"], "permission": name, "value": value,
                                "creates_file": not before, "diff": diff or "(no change)",
                                "note": "Writes the repository's .pi/autonomy.yaml. The change stays uncommitted "
                                        "until you commit it. Publishing (push branch, create PR) and GitHub "
                                        "issue and comment writes ignore this file: they read the policy "
                                        "committed on the default branch on GitHub, so this change has no "
                                        "effect on them until it is committed and pushed to the default branch."}}
        self._check_confirm(confirm, expected)
        self._write_policy(path, after)
        return {"ok": True, "stage": "done", "status": "POLICY_UPDATED",
                "audit": {"repository": repo["id"], "permission": name, "value": value}}

    @staticmethod
    def _write_policy(path: Path, text: str) -> None:
        top = _safe_policy_location(path)
        path.parent.mkdir(exist_ok=True)
        try:
            atomic_io.atomic_write_text(path, text, root=top)
        except atomic_io.UnsafePathError as exc:
            raise ControlError(409, "AUTONOMY_POLICY_PATH_UNSAFE", "the policy path is a symlink or leaves the checkout") from exc

    def _approve_mutation(self, plan, data, confirm):
        """Grant exactly the local-mutation permissions a repository's tasks are missing, and nothing else.

        The answer to an approval request (MUTATION_AUTHORIZATION_REQUIRED): create_worktrees and commit
        only. An existing policy keeps every other line; a new one denies every other permission. The
        change is written to the checkout, uncommitted: it authorizes nothing until committed on the base
        branch and pinned in the manifest. Push, PR, mark-ready, merge and deploy are never touched.
        """
        repo, checkout = self._repo(plan, data.get("repository"))
        path = autonomy_policy.policy_path(checkout)
        _safe_policy_location(path)
        before = path.read_text(encoding="utf-8") if path.is_file() else ""
        current = None
        if before:
            try:
                current = yaml.safe_load(before)
                if autonomy_policy.validate(current):
                    raise ValueError
            except (ValueError, yaml.YAMLError):
                raise ControlError(409, "AUTONOMY_POLICY_INVALID", "the checkout's policy does not validate; fix it in the file") from None
        permissions = (current or {}).get("permissions") or {}
        missing = [name for name in workspace_authorization.REQUIRED if permissions.get(name) is not True]
        if not missing:
            raise ControlError(409, "POLICY_UNCHANGED", "the checkout's policy already grants create_worktrees and commit; "
                               "commit it on the base branch and set the manifest pin to that commit")
        template = (Path(__file__).resolve().parent.parent / "templates" / "autonomy.yaml").read_text(encoding="utf-8")
        try:
            after = workspace_authorization.bounded_policy(before, template, missing)
        except ValueError as exc:
            raise ControlError(409, "AUTONOMY_POLICY_UNEDITABLE", str(exc)[:200]) from None
        tasks = sorted(t["id"] for t in plan["tasks"] if t["repository"] == repo["id"]
                       and t.get("kind") != "qualification" and t.get("mutation") != "none")
        diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                            fromfile=".pi/autonomy.yaml", tofile=".pi/autonomy.yaml"))
        expected = self._confirm("approve_mutation", repo["id"], missing, hashlib.sha256(before.encode()).hexdigest())
        if confirm is None:
            base = repo.get("base_branch", "main")
            return {"ok": True, "stage": "preview", "confirm": expected,
                    "preview": {"repository": repo["repository"], "grants": missing, "tasks": tasks,
                                "creates_file": not before, "diff": diff,
                                "never_granted": list(workspace_authorization.NEVER_GRANTED),
                                "already_granted_beyond_local": workspace_authorization.granted_beyond_local(current or {}),
                                "next_steps": [f"Commit the policy on {base} (Commit policy).",
                                               f"Set pinned_commit of {repo['id']} in the manifest to that commit.",
                                               "Schedule again with allow mutation: each blocked task starts a new "
                                               "attempt linked to the one that stopped."],
                                "note": "Grants only " + " and ".join(missing) + " in this checkout's .pi/autonomy.yaml, "
                                        "uncommitted. It authorizes nothing until it is committed on the base branch "
                                        "and pinned. It never grants push, pull requests, mark-ready, merge or deploy."}}
        self._check_confirm(confirm, expected)
        self._write_policy(path, after)
        return {"ok": True, "stage": "done", "status": "POLICY_UPDATED",
                "audit": {"repository": repo["id"], "granted": missing}}

    def _model_change(self, action, data, confirm, apply):
        family, tier = data.get("family"), data.get("tier")
        if family not in model_profiles.FAMILIES or tier not in model_profiles.TIERS:
            raise ControlError(400, "MODEL_CHANGE_INVALID", "unknown family or tier")
        path = model_profiles.default_config_path()
        before = path.read_text(encoding="utf-8") if path.exists() else ""
        summary = model_profiles._summary(path, model_profiles._load_raw(path) if path.exists() else None)
        current = {"model": summary["models"][family][tier], "capacity": summary["ambiguity_capacities"][family][tier]}
        target = apply(summary)
        expected = self._confirm(action, family, tier, target, hashlib.sha256(before.encode()).hexdigest())
        if confirm is None:
            return {"ok": target != current, "stage": "preview", "confirm": expected,
                    "preview": {"family": family, "tier": tier, "current": current, "new": target,
                                "config_path": "user model config", "note": "Applies to the next run that routes to this tier."}}
        self._check_confirm(confirm, expected)
        return None

    def _set_model(self, plan, data, confirm):
        model, capacity = data.get("model"), data.get("capacity")
        if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
            raise ControlError(400, "MODEL_CHANGE_INVALID", "invalid model id")
        if isinstance(capacity, bool) or not isinstance(capacity, int):
            raise ControlError(400, "MODEL_CHANGE_INVALID", "capacity must be an integer")

        def target(summary):
            known = {c["model"] for c in self._model_choices(summary)[data["family"]]}
            if model not in known:
                raise ControlError(400, "MODEL_CHANGE_INVALID", "only models already bound in this family can be chosen here")
            return {"model": model, "capacity": capacity}
        preview = self._model_change("set_model", data, confirm, target)
        if preview is not None:
            return preview
        try:
            with redirect_stdout(io.StringIO()):
                model_profiles.cmd_set(model_profiles.default_config_path(), data["family"], data["tier"], model,
                                       None, None, capacity)
        except (ValueError, engine.RoutingAssetError) as exc:
            return {"ok": False, "stage": "refused", "error_code": "MODEL_CHANGE_INVALID", "error": str(exc)[:300]}
        return {"ok": True, "stage": "done", "status": "MODEL_UPDATED",
                "audit": {"family": data["family"], "tier": data["tier"], "model": model, "capacity": capacity}}

    def _reset_model(self, plan, data, confirm):
        def target(summary):
            entry = model_profiles._base_policy()["model_profiles"][data["family"]]["capacity_models"][data["tier"]]
            return {"model": entry["model"], "capacity": entry["ambiguity_capacity"]}
        preview = self._model_change("reset_model", data, confirm, target)
        if preview is not None:
            return preview
        with redirect_stdout(io.StringIO()):
            model_profiles.cmd_reset(model_profiles.default_config_path(), data["family"], data["tier"], False)
        return {"ok": True, "stage": "done", "status": "MODEL_RESET",
                "audit": {"family": data["family"], "tier": data["tier"]}}

    def _commit_policy(self, plan, data, confirm):
        """Commit only .pi/autonomy.yaml, only on the repository's base branch, never pushed."""
        repo, checkout = self._repo(plan, data.get("repository"))
        path = autonomy_policy.policy_path(checkout)
        relative = str(path.relative_to(checkout))

        def git(*args, check=False):
            return subprocess.run(["git", "-C", str(checkout), *args], shell=False, capture_output=True,
                                  text=True, timeout=30)
        branch = git("symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
        base = repo.get("base_branch", "main")
        if branch != base:
            raise ControlError(409, "POLICY_COMMIT_BRANCH",
                               f"the checkout is on {branch or 'a detached HEAD'}, not {base}; switch to {base} to "
                               "commit the policy, so it never lands on a work branch")
        if not path.is_file() or not git("status", "--porcelain", "--", relative).stdout.strip():
            raise ControlError(409, "POLICY_UNCHANGED", "the policy has no uncommitted change")
        try:
            if autonomy_policy.validate(yaml.safe_load(path.read_text(encoding="utf-8"))):
                raise ValueError
        except (OSError, ValueError, yaml.YAMLError):
            raise ControlError(409, "AUTONOMY_POLICY_INVALID", "the policy does not validate") from None
        tracked = git("ls-files", "--error-unmatch", "--", relative).returncode == 0
        diff = git("diff", "HEAD", "--", relative).stdout if tracked else path.read_text(encoding="utf-8")
        head = git("rev-parse", "HEAD").stdout.strip()
        message = "Update PICO autonomy policy"
        expected = self._confirm("commit_policy", repo["id"], head, hashlib.sha256(diff.encode()).hexdigest())
        if confirm is None:
            return {"ok": True, "stage": "preview", "confirm": expected,
                    "preview": {"repository": repo["repository"], "branch": branch, "head": head, "message": message,
                                "diff": diff if tracked else f"(new file {relative})\n{diff}",
                                "note": "Commits only this file with your Git identity. Nothing is pushed."}}
        self._check_confirm(confirm, expected)
        if not tracked:
            added = git("add", "--", relative)
            if added.returncode:
                return {"ok": False, "stage": "refused", "error_code": "POLICY_COMMIT_FAILED", "error": "git add failed"}
        done = git("commit", "--only", "-m", message, "--", relative)
        if done.returncode:
            return {"ok": False, "stage": "refused", "error_code": "POLICY_COMMIT_FAILED",
                    "error": (done.stderr or done.stdout).strip().splitlines()[-1][:300] if (done.stderr or done.stdout) else "git commit failed"}
        commit = git("rev-parse", "HEAD").stdout.strip()
        return {"ok": True, "stage": "done", "status": "POLICY_COMMITTED", "commit": commit,
                "audit": {"repository": repo["id"], "commit": commit}}
