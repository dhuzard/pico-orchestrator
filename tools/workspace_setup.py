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


class SetupControls(garden_controls.Controls):
    def __init__(self, root: Path, port: int, *, github=workspace_init.verify_github):
        super().__init__(root, port)
        if not root.is_dir():
            raise ValueError("setup root must be a directory")
        self.root = root.resolve()
        self.github = github

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
                "skipped": found.skipped, "unreadable": found.unreadable, "truncated": found.truncated}

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
