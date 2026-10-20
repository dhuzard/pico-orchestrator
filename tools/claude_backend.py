"""Resolve the Claude executable used by pi-claude-bridge's Agent SDK."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

# Pinned instead of an identity when no Claude Code is installed. Preflight maps
# it to the reroutable MODEL_BACKEND_UNAVAILABLE, so Codex-only routes still run.
UNAVAILABLE_IDENTITY = {"backend": "claude-code", "unavailable": True}


def agent_dir() -> Path:
    return Path(os.environ.get("PI_CODING_AGENT_DIR", "~/.pi/agent")).expanduser()


def _config(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("invalid Claude bridge configuration")
    return data


def configured_executable(cwd: Path) -> str | None:
    global_provider = _config(agent_dir() / "claude-bridge.json").get("provider") or {}
    project_provider = _config(cwd / ".pi" / "claude-bridge.json").get("provider") or {}
    if not isinstance(global_provider, dict) or not isinstance(project_provider, dict):
        raise ValueError("invalid Claude bridge provider configuration")
    value = {**global_provider, **project_provider}.get("pathToClaudeCodeExecutable")
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError("invalid Claude bridge executable")
    return value


def resolve(cwd: Path, *, executable: str | None = None, which=None) -> Path:
    which = shutil.which if which is None else which
    configured = executable if executable is not None else configured_executable(cwd)
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            found = which(configured)
            if not found:
                raise FileNotFoundError(configured)
            path = Path(found)
    else:
        candidates = sorted((agent_dir() / "npm" / "node_modules").glob("@anthropic-ai/claude-agent-sdk-*/claude"))
        if not candidates:
            raise FileNotFoundError("Claude Agent SDK bundled executable")
        path = candidates[0]
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise FileNotFoundError(str(resolved))
    return resolved


def identity(path: Path, *, run=subprocess.run) -> dict:
    path = path.resolve(strict=True)
    result = run([str(path), "--version"], shell=False, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise ValueError("Claude version query failed")
    stat = path.stat()
    return {"backend": "claude-code", "backend_executable": str(path),
            "backend_version_output": result.stdout.strip(),
            "backend_device": stat.st_dev, "backend_inode": stat.st_ino}


def configure_host(cwd: Path, *, which=None) -> dict:
    """Pin the bridge to the host Claude before Pi loads extensions.

    The bridge is pointed at the host's stable launcher path (for example the
    `~/.local/bin/claude` symlink), not the versioned file behind it, so plain
    `pi` keeps working after Claude Code updates itself. The returned identity
    is the resolved file: an update during a session changes what the path
    resolves to, and preflight then fails closed with MODEL_BACKEND_MISMATCH.
    Routed AskClaude calls default to isolated sessions, so a worker never sees
    a shared session or the Pi conversation unless a caller opts in explicitly.
    """
    which = shutil.which if which is None else which
    host = which("claude")
    if not host:
        raise FileNotFoundError("claude")
    host = os.path.abspath(host)
    path = resolve(cwd, executable=host)
    config_path = agent_dir() / "claude-bridge.json"
    data = _config(config_path)
    provider = data.get("provider") or {}
    ask = data.get("askClaude") or {}
    if not isinstance(provider, dict) or not isinstance(ask, dict):
        raise ValueError("invalid Claude bridge provider configuration")
    provider["pathToClaudeCodeExecutable"] = host
    ask["defaultIsolated"] = True
    data["provider"] = provider
    data["askClaude"] = ask
    # A project override wins in the real bridge, so reject a divergent one.
    project = _config(cwd / ".pi" / "claude-bridge.json").get("provider") or {}
    override = project.get("pathToClaudeCodeExecutable") if isinstance(project, dict) else None
    if override and resolve(cwd, executable=override) != path:
        raise ValueError("MODEL_BACKEND_MISMATCH: project bridge executable overrides host")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    # A unique temporary file per writer: concurrent launches must not collide.
    fd, temp = tempfile.mkstemp(dir=config_path.parent, prefix=config_path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(data, indent=2) + "\n")
        os.replace(temp, config_path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    return identity(path)


if __name__ == "__main__":
    import sys
    if not shutil.which("claude"):
        print("Claude Code is not installed: routes with a Claude step will be rerouted or "
              "blocked (MODEL_BACKEND_UNAVAILABLE); Codex-only routes remain available.", file=sys.stderr)
        print(json.dumps(UNAVAILABLE_IDENTITY))
        raise SystemExit(0)
    try:
        print(json.dumps(configure_host(Path(sys.argv[1]).resolve())))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Claude backend configuration failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
