"""Local browser adapter for the reviewed workspace manifest builder.

The builder owns discovery, identity resolution, schema validation and exclusive
save. This adapter only binds a browser preview to the exact bytes to save.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import subprocess
from pathlib import Path

import garden_controls
import workspace_init
import workspace_plan
import workspace_run

MANIFEST_SUFFIXES = (".yaml", ".yml", ".json")
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_MANIFESTS = 256


def _git_probe(checkout: Path, *args: str, text: bool = False):
    try:
        return subprocess.run(["git", "-C", str(checkout), *args], shell=False,
                              capture_output=True, text=text, timeout=10)
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return None


class SetupControls(garden_controls.Controls):
    def __init__(self, root: Path, port: int, *, state: Path | None = None,
                 github=workspace_init.verify_github, access_check=workspace_plan.gh_access):
        super().__init__(root, port)
        if not root.is_dir():
            raise ValueError("setup root must be a directory")
        self.root = root.resolve()
        self.state = state or self.root / ".pico-state"
        self.github = github
        self.access_check = access_check

    def manifests(self) -> list[str]:
        """List direct, regular manifest files; never follow a symlink out of the setup root."""
        names = []
        for path in self.root.iterdir():
            try:
                if path.suffix.lower() in MANIFEST_SUFFIXES and not path.is_symlink() and path.is_file() \
                        and path.stat().st_size <= MAX_MANIFEST_BYTES:
                    names.append(path.name)
            except OSError:  # A file may disappear during discovery.
                continue
        return sorted(names)[:MAX_MANIFESTS]

    def inspect_manifest(self, name: str) -> dict:
        """Reopen an existing manifest without copying, rewriting, or activating it."""
        import watch
        conflicts = workspace_init.environment_conflicts(dict(os.environ))
        if conflicts:
            raise garden_controls.ControlError(409, "WORKSPACE_INIT_ENV_CONFLICT", "; ".join(conflicts))
        if not isinstance(name, str) or not name or len(name) > 255 or Path(name).name != name \
                or Path(name).suffix.lower() not in MANIFEST_SUFFIXES:
            raise garden_controls.ControlError(400, "WORKSPACE_IMPORT_NAME_INVALID", "choose a manifest file in the setup root")
        path = self.root / name
        try:
            available = not path.is_symlink() and path.is_file() and path.stat().st_size <= MAX_MANIFEST_BYTES
        except OSError:
            available = False
        if not available:
            raise garden_controls.ControlError(404, "WORKSPACE_IMPORT_UNAVAILABLE", "manifest file is absent, a symlink, or too large")
        try:
            raw = workspace_plan.load_manifest(path)
            if not isinstance(raw, dict) or not workspace_init.SLUG_RE.fullmatch(str(raw.get("id", ""))):
                raise workspace_plan.WorkspaceError("workspace id is invalid")
            record = workspace_run.load_workspace(self.state / "workspaces" / f"{raw['id']}.json")
            observed = workspace_run.observed_from_record(record or {}, manifest=path)
            plan = workspace_plan.plan(path, access_check=self.access_check, observed=observed)
            # Import locally: watch's existing view owns the scheduler state and run mapping.
            view = watch.workspace_view(self.state, plan=plan)
        except (OSError, UnicodeError, workspace_plan.WorkspaceError, workspace_run.WorkspaceRunError,
                watch.WorkspaceViewError) as exc:
            raise garden_controls.ControlError(422, "WORKSPACE_IMPORT_INVALID",
                                               "the manifest or its workspace record cannot be read") from exc
        repositories = []
        for repo in plan["repositories"]:
            checkout = Path(repo["resolved_path"]) if repo.get("resolved_path") else None
            head = workspace_run._head(checkout) if checkout else None
            if checkout:
                status = _git_probe(checkout, "status", "--porcelain=v1")
                branch = _git_probe(checkout, "symbolic-ref", "--quiet", "--short", "HEAD", text=True)
            else:
                status = branch = None
            repositories.append({"id": repo["id"], "repository": repo["repository"], "path": repo["path"],
                                 "resolved_path": repo.get("resolved_path"), "base_branch": repo.get("base_branch"),
                                 "head": head, "branch": branch.stdout.strip() if branch and branch.returncode == 0 else None,
                                 "pinned_commit": repo["pinned_commit"],
                                 "dirty": None if status is None or status.returncode else bool(status.stdout),
                                 "access": repo["access"], "blocker_code": repo.get("blocker_code")})
        return {"ok": True, "name": name, "path": str(path), "workspace": view,
                "repositories": repositories, "blockers": plan["blockers"],
                "read_only": True}

    def discovery(self) -> dict:
        conflicts = workspace_init.environment_conflicts(dict(os.environ))
        if conflicts:
            raise garden_controls.ControlError(409, "WORKSPACE_INIT_ENV_CONFLICT", "; ".join(conflicts))
        found = workspace_init.discover(self.root)
        candidates = []
        for candidate in found.candidates:
            path = candidate.path
            def git(*args: str, checkout: Path = path) -> str | None:
                result = subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, text=True,
                                        timeout=10, shell=False)
                return result.stdout.strip() if result.returncode == 0 else None
            dirty = git("status", "--porcelain")
            candidates.append({"path": str(path), "label": candidate.label, "identities": candidate.identities,
                               "ambiguous": candidate.ambiguous, "head": git("rev-parse", "HEAD"),
                               "branch": git("symbolic-ref", "--quiet", "--short", "HEAD"),
                               "dirty": None if dirty is None else bool(dirty)})
        return {"ok": True, "root": str(self.root), "candidates": candidates,
                "skipped": found.skipped, "unreadable": found.unreadable, "truncated": found.truncated,
                "manifests": self.manifests()}

    def _prepare(self, data: dict):
        conflicts = workspace_init.environment_conflicts(dict(os.environ))
        if conflicts:
            raise garden_controls.ControlError(409, "WORKSPACE_INIT_ENV_CONFLICT", "; ".join(conflicts))
        raw = data.get("spec")
        if not isinstance(raw, dict):
            raise garden_controls.ControlError(400, "WORKSPACE_INIT_SPEC_INVALID", "spec must be an object")
        if raw.get("root") != str(self.root):
            raise garden_controls.ControlError(400, "WORKSPACE_INIT_ROOT_CHANGED", "use this setup page's root")
        if data.get("output") is not None and not isinstance(data["output"], str):
            raise garden_controls.ControlError(400, "WORKSPACE_INIT_SPEC_INVALID", "output must be a path string")
        try:
            spec = workspace_init.validate_spec(raw, base=self.root)
            destination = workspace_init.destination_path(data.get("output"), root=self.root,
                                                          workspace_id=spec.id, cwd=self.root)
            if destination.parent != self.root:
                raise garden_controls.ControlError(400, "WORKSPACE_INIT_OUTPUT_OUTSIDE_ROOT",
                                                   "the manifest must be saved in the setup root")
            return workspace_init.prepare(spec, destination, github=self.github)
        except workspace_init.InitError as exc:
            raise garden_controls.ControlError(409, exc.code, exc.detail) from exc

    def act_setup(self, data: dict) -> dict:
        action = data.get("action")
        if action not in {"preview", "save"}:
            raise garden_controls.ControlError(400, "WORKSPACE_SETUP_ACTION_UNKNOWN", "use preview or save")
        if not self._lock.acquire(blocking=False):
            raise garden_controls.ControlError(409, "CONTROL_BUSY", "another setup action is running")
        try:
            prepared = self._prepare(data)
            digest = hashlib.sha256(prepared.text.encode()).hexdigest()
            binding = self._confirm("workspace_setup", str(prepared.destination), digest)
            if action == "save":
                supplied = data.get("confirm")
                if not isinstance(supplied, str) or not hmac.compare_digest(supplied, binding):
                    raise garden_controls.ControlError(409, "CONTROL_PREVIEW_STALE", "preview this manifest again")
                try:
                    workspace_init.save_exclusive(prepared.destination, prepared.text)
                except workspace_init.InitError as exc:
                    raise garden_controls.ControlError(409, exc.code, exc.detail) from exc
                return {"ok": True, "stage": "saved", "path": str(prepared.destination)}
            return {"ok": True, "stage": "preview", "confirm": binding,
                    "manifest": prepared.manifest, "destination": str(prepared.destination),
                    "repositories": [{k: str(value) if isinstance(value, Path) else value
                                      for k, value in item.items() if k != "git_common_dir"}
                                     for item in prepared.resolutions],
                    "warnings": prepared.warnings, "plan_status": prepared.plan["status"],
                    "blockers": prepared.plan["blockers"], "text": prepared.text}
        finally:
            self._lock.release()
