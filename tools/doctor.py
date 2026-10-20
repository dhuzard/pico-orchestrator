#!/usr/bin/env python3
"""Read-only readiness report for pi-coding-orchestrator."""
from __future__ import annotations

import argparse
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

import yaml  # noqa: E402

import model_profiles  # noqa: E402
import routing_engine as engine  # noqa: E402


def _run(argv: list[str], *, cwd: Path | None = None) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            argv, cwd=cwd, shell=False, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "output": None, "error": type(exc).__name__}
    output = (proc.stdout or proc.stderr or "").strip()
    return {"ok": proc.returncode == 0, "output": output or None, "error": None}


def _dependency(name: str, version_args: list[str] | None = None) -> dict[str, Any]:
    path = shutil.which(name)
    result: dict[str, Any] = {"available": path is not None, "path": path, "version": None}
    if path and version_args:
        probe = _run([path, *version_args])
        if probe["ok"]:
            result["version"] = probe["output"]
    return result


def _git_target(target: Path) -> dict[str, Any]:
    probe = _run(["git", "-C", str(target), "rev-parse", "--show-toplevel"])
    return {"ok": probe["ok"], "root": probe["output"] if probe["ok"] else None}


def _auth_status() -> dict[str, Any]:
    gh = shutil.which("gh")
    claude = shutil.which("claude")
    gh_probe = _run([gh, "auth", "status"]) if gh else {"ok": False, "output": None}
    claude_probe = (
        _run([claude, "auth", "status", "--json"])
        if claude
        else {"ok": False, "output": None}
    )
    claude_logged_in = False
    if claude_probe["ok"] and claude_probe["output"]:
        try:
            payload = json.loads(claude_probe["output"])
            claude_logged_in = isinstance(payload, dict) and payload.get("loggedIn") is True
        except json.JSONDecodeError:
            claude_logged_in = False
    return {
        "github": {"authenticated": bool(gh_probe["ok"])},
        "claude": {"authenticated": claude_logged_in},
    }


def _orchestrator_revision() -> str | None:
    # Launcher runtimes are git-archive snapshots that record their commit.
    marker = ROOT / ".pico-runtime"
    if marker.is_file():
        value = marker.read_text(encoding="utf-8").strip()
        return value if re.fullmatch(r"[0-9a-f]{40}", value) else None
    probe = _run(["git", "-C", str(ROOT), "rev-parse", "HEAD"])
    return probe["output"] if probe["ok"] else None


# Pi packages that provide the worker tools routed plans invoke.
PI_PACKAGES = {"pi_claude_bridge": "pi-claude-bridge", "pi_subagent_workflows": "pi-subagent-workflows"}


def _pi_packages() -> dict[str, bool]:
    agent = Path(os.environ.get("PI_CODING_AGENT_DIR", "~/.pi/agent")).expanduser()
    configured: list[str] = []
    try:
        settings = json.loads((agent / "settings.json").read_text(encoding="utf-8"))
        packages = settings.get("packages") if isinstance(settings, dict) else None
        configured = [item for item in packages or [] if isinstance(item, str)]
    except (OSError, json.JSONDecodeError):
        pass
    return {check: (agent / "npm" / "node_modules" / name).is_dir()
            or any(name in item for item in configured)
            for check, name in PI_PACKAGES.items()}


def _project_pin(target: Path) -> str | None:
    path = target / ".pi" / "orchestrator.lock"
    if not path.exists():
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return "UNREADABLE"
    return value or "INVALID"


def build_report(target: Path) -> dict[str, Any]:
    target = target.resolve()
    model_path = model_profiles.default_config_path()
    try:
        config = model_profiles._load_raw(model_path) if model_path.exists() else None
        model_summary = model_profiles._summary(model_path, config)
        models_ok = True
        model_error = None
    except (ValueError, OSError, engine.RoutingAssetError, yaml.YAMLError) as exc:
        model_summary = None
        models_ok = False
        model_error = str(exc)

    dependencies = {
        "git": _dependency("git", ["--version"]),
        "python3": _dependency("python3", ["--version"]),
        "pi": _dependency("pi", ["--version"]),
        "gh": _dependency("gh", ["--version"]),
        "claude": _dependency("claude", ["--version"]),
    }
    auth = _auth_status()
    target_status = _git_target(target)
    state_dir = os.environ.get("PI_ORCHESTRATOR_STATE_DIR")
    project_pin = _project_pin(target)
    packages = _pi_packages()
    checks = {
        "models": models_ok,
        "target_git_repository": bool(target_status["ok"]),
        "git": dependencies["git"]["available"],
        "python3": dependencies["python3"]["available"],
        "pi": dependencies["pi"]["available"],
        **packages,
        "gh": dependencies["gh"]["available"],
        "claude": dependencies["claude"]["available"],
        "github_auth": auth["github"]["authenticated"],
        "claude_auth": auth["claude"]["authenticated"],
        "state_dir_absolute": state_dir is None or Path(state_dir).expanduser().is_absolute(),
        "project_pin_valid": project_pin is None or bool(re.fullmatch(r"[0-9a-fA-F]{40}", project_pin)),
    }
    return {
        "schema_version": 1,
        "ok": all(checks.values()),
        "checks": checks,
        "target": {"path": str(target), **target_status},
        "orchestrator": {
            "root": str(ROOT),
            "revision": _orchestrator_revision(),
            "state_dir": state_dir,
            "project_pin": project_pin,
        },
        "dependencies": dependencies,
        "authentication": auth,
        "model_profiles": model_summary,
        "model_error": model_error,
    }


def render_text(report: dict[str, Any]) -> str:
    lines = []
    for name, ok in report["checks"].items():
        lines.append(f"{'OK  ' if ok else 'FAIL'} {name}")
    lines.append(f"orchestrator revision: {report['orchestrator']['revision'] or 'unknown'}")
    lines.append(f"project pin: {report['orchestrator']['project_pin'] or '(none)'}")
    lines.append(f"state dir: {report['orchestrator']['state_dir'] or '(repository-local default)'}")
    models = report.get("model_profiles")
    if models:
        lines.append("Effective model profiles:")
        for family, tiers in models["models"].items():
            lines.append(f"  {family}: " + ", ".join(f"{tier}={model}" for tier, model in tiers.items()))
    if report.get("model_error"):
        lines.append(f"model configuration error: {report['model_error']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", type=Path, default=Path("."))
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    report = build_report(args.target)
    if args.as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
