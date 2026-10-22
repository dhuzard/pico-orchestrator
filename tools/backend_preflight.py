#!/usr/bin/env python3
"""Read-only preflight of the exact backend/model bindings returned by route_task.

This checks local invocation readiness. It never invokes inference and never
substitutes a route or model. Account-specific model entitlement is not
provable from a local auth status and remains explicitly unverified.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import routing_engine as engine
import claude_backend

VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:\b|$)")


def _version(value: str) -> tuple[int, int, int] | None:
    match = VERSION_RE.match(value.strip())
    return tuple(map(int, match.groups())) if match else None


def _blocked(code: str, step: dict, model: str, **details: Any) -> dict:
    return {"ok": False, "status": "BLOCKED", "blocker_code": code,
            "step_id": step["id"], "backend": step["family"], "model": model, **details}


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, shell=False, capture_output=True, text=True, timeout=10)


PIN_ENV = "PI_ORCHESTRATOR_CLAUDE_BACKEND_IDENTITY"
WORKER_SANDBOX_ENV = "PI_ORCHESTRATOR_WORKER_SANDBOX"
IDENTITY_KEYS = ("backend_executable", "backend_version_output", "backend_device", "backend_inode")
LAUNCH_GUIDANCE = (
    "Start Pi through a canonical launcher, which pins the Claude backend identity before "
    "extensions load: scripts/use-latest.sh run <target-repo> (cached latest orchestrator) or "
    "scripts/run-local.sh <target-repo> (this checkout). Plain `pi -e ...` does not set "
    f"{PIN_ENV}."
)
# Blockers that only mean "this candidate route cannot run here". Routers may
# exclude such a route and re-select another policy-valid candidate. Every other
# blocker (identity mismatch/uninitialized pin, invalid route, invalid config)
# is fail-closed and must never trigger a reroute.
REROUTABLE_BLOCKERS = frozenset({
    "MODEL_BACKEND_UNAVAILABLE",
    "MODEL_AUTH_UNAVAILABLE",
    "MODEL_BACKEND_VERSION_UNSUPPORTED",
})
_FROM_ENV = object()


def pinned_identity_state(raw: str | None) -> tuple[str, dict | None]:
    """Classify the launcher pin as ("pinned", identity), ("unavailable", None), ("missing", None) or ("malformed", None)."""
    if raw is None or not raw.strip():
        return "missing", None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return "malformed", None
    if value == claude_backend.UNAVAILABLE_IDENTITY:
        return "unavailable", None
    if (not isinstance(value, dict) or not isinstance(value.get("backend_executable"), str)
            or not value["backend_executable"] or not isinstance(value.get("backend_version_output"), str)
            or any(type(value.get(key)) is not int for key in ("backend_device", "backend_inode"))):
        return "malformed", None
    return "pinned", value


def _identity_matches(actual: dict, expected: dict) -> bool:
    return all(actual.get(key) == expected.get(key) for key in IDENTITY_KEYS)


def preflight(route: dict, available_tools: list[str], *, executable: str | None = None,
              run=_run, which=shutil.which, compatibility: dict | None = None,
              cwd: Path | None = None, pinned_identity: Any = _FROM_ENV) -> dict:
    """Return structured readiness for every routed step, without creating worktrees.

    `pinned_identity` is the launcher-pinned Claude identity as raw JSON text
    (or None); by default it is read from PI_ORCHESTRATOR_CLAUDE_BACKEND_IDENTITY.
    """
    raw_pin = os.environ.get(PIN_ENV) if pinned_identity is _FROM_ENV else pinned_identity
    assets = engine.load_policy_assets(ROOT)
    issues = engine.validate_route_task_result(route, assets)
    if issues:
        return {"ok": False, "status": "BLOCKED", "blocker_code": "ROUTING_RESULT_INVALID",
                "errors": [issue.as_dict() for issue in issues]}
    try:
        rules = compatibility or engine.load_backend_compatibility(
            ROOT,
            effective_policy=assets.policy,
        )
    except engine.RoutingAssetError:
        return {"ok": False, "status": "BLOCKED", "blocker_code": "MODEL_BACKEND_CONFIG_INVALID"}
    assignments = {item["step_id"]: item["assigned_capacity_tier"] for item in route["capacity_assignments"]}
    step_models: dict[str, str] = {}
    first_claude: tuple[dict, str] | None = None
    for step in route["execution_plan"]["steps"]:
        tier = assignments[step["id"]]
        model = route["backend_bindings"][step["family"]]["capacity_models"][tier]["model"]
        step_models[step["id"]] = model
        if step["family"] == "claude" and first_claude is None:
            first_claude = (step, model)
    # Without the launcher pin there is no trusted identity to compare against.
    # For any route that contains Claude, fail closed before tool-availability
    # probing so a missing pin is never converted into a reroutable unavailable
    # backend blocker.
    pin_state, pinned = pinned_identity_state(raw_pin)
    if first_claude is not None and pin_state == "unavailable":
        # The launcher found no Claude Code: a candidate-availability blocker
        # that may be rerouted to a policy-valid candidate without Claude.
        step, model = first_claude
        return _blocked("MODEL_BACKEND_UNAVAILABLE", step, model, required_tool="claude",
                        identity_state=pin_state)
    if first_claude is not None and pinned is None:
        step, model = first_claude
        return _blocked("BACKEND_IDENTITY_UNINITIALIZED", step, model,
                        identity_state=pin_state, remediation=LAUNCH_GUIDANCE)
    checked = []
    for step in route["execution_plan"]["steps"]:
        family = step["family"]
        tier = assignments[step["id"]]
        binding = route["backend_bindings"][family]
        model = step_models[step["id"]]
        tool = binding["tool"]
        if tool not in available_tools:
            return _blocked("MODEL_BACKEND_UNAVAILABLE", step, model, required_tool=tool)
        evidence = {"step_id": step["id"], "backend": family, "tool": tool,
                    "model": model, "model_access": "unverified"}
        if family == "claude":
            backend_model = rules["claude_code_model_ids"].get(model)
            if backend_model is None:
                return _blocked("MODEL_IDENTIFIER_UNSUPPORTED", step, model)
            if not isinstance(backend_model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", backend_model):
                return _blocked("MODEL_BACKEND_CONFIG_INVALID", step, model)
            evidence["backend_model"] = backend_model
            try:
                binary = executable or str(claude_backend.resolve(cwd or Path.cwd(), which=which))
            except (OSError, ValueError, json.JSONDecodeError):
                return _blocked("MODEL_BACKEND_UNAVAILABLE", step, model, required_tool=tool)
            evidence["backend_executable"] = binary
            try:
                version_result = run([binary, "--version"])
            except (OSError, subprocess.TimeoutExpired):
                return _blocked("MODEL_BACKEND_UNAVAILABLE", step, model, required_tool=tool)
            installed = _version(version_result.stdout) if version_result.returncode == 0 else None
            if installed is None:
                return _blocked("MODEL_BACKEND_UNAVAILABLE", step, model, required_tool=tool)
            installed_text = ".".join(map(str, installed))
            evidence["installed_backend_version"] = installed_text
            evidence["backend_version_output"] = version_result.stdout.strip()
            try:
                stat = Path(binary).stat()
                evidence["backend_device"] = stat.st_dev
                evidence["backend_inode"] = stat.st_ino
            except OSError:
                return _blocked("MODEL_BACKEND_UNAVAILABLE", step, model, required_tool=tool)
            if not _identity_matches(evidence, pinned):
                return _blocked("MODEL_BACKEND_MISMATCH", step, model, backend_model=backend_model,
                                installed_backend_version=installed_text,
                                remediation="The Claude executable differs from the identity pinned at launch. "
                                            "Restart Pi through the canonical launcher after reviewing the change.")
            required_text = rules["claude_code_minimum_versions"].get(model)
            if required_text is not None:
                required = _version(required_text)
                if required is None:
                    return _blocked("MODEL_BACKEND_CONFIG_INVALID", step, model)
                if installed < required:
                    return _blocked("MODEL_BACKEND_VERSION_UNSUPPORTED", step, model,
                                    backend_model=backend_model,
                                    installed_backend_version=installed_text,
                                    required_backend_version=f">={required_text}")
            # The worker sandbox settings (#63) need a Claude Code that honours them; unless the user opted out of
            # the sandbox explicitly, an older executable is refused with the same version blocker.
            sandbox_text = rules.get("claude_code_worker_sandbox_minimum_version")
            if sandbox_text is not None and os.environ.get(WORKER_SANDBOX_ENV, "").strip() != "0":
                sandbox_minimum = _version(sandbox_text) if isinstance(sandbox_text, str) else None
                if sandbox_minimum is None:
                    return _blocked("MODEL_BACKEND_CONFIG_INVALID", step, model)
                if installed < sandbox_minimum:
                    return _blocked("MODEL_BACKEND_VERSION_UNSUPPORTED", step, model,
                                    backend_model=backend_model,
                                    installed_backend_version=installed_text,
                                    required_backend_version=f">={sandbox_text}",
                                    requirement="worker_sandbox")
            try:
                auth_result = run([binary, "auth", "status", "--json"])
                auth = json.loads(auth_result.stdout) if auth_result.returncode == 0 else {}
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
                auth = {}
            if auth.get("loggedIn") is not True:
                return _blocked("MODEL_AUTH_UNAVAILABLE", step, model,
                                installed_backend_version=installed_text)
            evidence["authentication"] = "available"
        checked.append(evidence)
    return {"ok": True, "status": "READY", "policy_version": route["policy_version"],
            "route": route["selected_route"], "checked_steps": checked}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise TypeError("payload must be an object")
        if payload.get("verify_backend"):
            expected = payload["backend_identity"]
            if not isinstance(expected, dict) or not all(key in expected for key in IDENTITY_KEYS):
                raise TypeError("backend_identity must be a preflighted identity")
            try:
                actual = claude_backend.identity(claude_backend.resolve(Path(payload["cwd"])))
                ok = _identity_matches(actual, expected)
            except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
                actual, ok = {}, False
            result = {"ok": ok, "status": "READY" if ok else "BLOCKED",
                      "blocker_code": None if ok else "MODEL_BACKEND_MISMATCH", "actual": actual}
        else:
            if not isinstance(payload.get("cwd"), str) or not isinstance(payload.get("available_tools"), list):
                raise TypeError("cwd and available_tools are required")
            result = preflight(payload["route_task_result"], payload["available_tools"],
                               cwd=Path(payload["cwd"]))
    except (KeyError, TypeError, ValueError, OSError, engine.RoutingAssetError, json.JSONDecodeError):
        result = {"ok": False, "status": "BLOCKED", "blocker_code": "ROUTING_RESULT_INVALID"}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
