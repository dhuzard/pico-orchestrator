#!/usr/bin/env python3
"""No-model diagnostic for a reviewed native Claude sandbox candidate (Linux x86-64)."""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import os
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

# Internal multicall invocation is only verified for this exact native build.
CLAUDE_SHA256 = "708c77b204db30086ff6f1ed34577bda03e1be74ced80557cbf7b54249ebf0bd"
BWRAP_SHA256 = "523da3e7399044be5163aee6f57a77a6bef7454376e28f0a0627920bae1b76b6"


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def validate_bwrap(path: Path) -> None:
    """Reject executable or ancestors that an unprivileged account can replace."""
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o6022:
        raise ValueError("bubblewrap must be root-owned, non-setuid and not group/other writable")
    for parent in path.parents:
        entry = parent.stat()
        # A root-owned sticky /tmp protects a root-owned immediate child from
        # replacement and permits a disposable operator test. Production uses /opt.
        sticky_tmp = parent == Path("/tmp") and bool(entry.st_mode & stat.S_ISVTX)
        if (not stat.S_ISDIR(entry.st_mode) or entry.st_uid != 0
                or (entry.st_mode & 0o022 and not sticky_tmp)):
            raise ValueError("bubblewrap parent directories must be root-owned and protected from replacement")
    try:
        os.getxattr(path, "security.capability")
    except OSError as exc:
        if exc.errno != errno.ENODATA:
            raise ValueError("cannot verify bubblewrap file capabilities") from exc
    else:
        raise ValueError("bubblewrap must have no file capabilities")


def child(base: Path, uid: int, label: str, port: int) -> int:
    checks: dict[str, bool] = {}
    checks["uid"] = os.getuid() == uid and uid != 0
    checks["apparmor"] = Path("/proc/self/attr/current").read_text().strip() == label
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    for key in ("CapEff", "CapPrm", "CapAmb"):
        checks[key] = status.get(key, "").strip() == "0000000000000000"
    checks["NoNewPrivs"] = status.get("NoNewPrivs", "").strip() == "1"
    checks["Seccomp"] = status.get("Seccomp", "").strip() == "2"
    try:
        (base / "workspace" / "write").write_text("ok")
        checks["workspace_write"] = True
        checks["git_init"] = subprocess.run(["/usr/bin/git", "init", "-q", str(base / "workspace" / "repo")],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        checks["workspace_write"] = checks["git_init"] = False
    try:
        checks["masked_read"] = (base / "secret").read_text() == ""
    except OSError as exc:
        checks["masked_read"] = exc.errno == errno.EACCES
    try:
        (base / "protected" / "sandbox-write").write_text("x")
        checks["protected_write_denied"] = False
    except OSError as exc:
        checks["protected_write_denied"] = exc.errno in (errno.EROFS, errno.EACCES)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM):
            checks["unix_socket_denied"] = False
    except OSError as exc:
        checks["unix_socket_denied"] = exc.errno in (errno.EPERM, errno.EACCES)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            sock.connect(("127.0.0.1", port))
            checks["host_loopback_denied"] = False
    except OSError as exc:
        checks["host_loopback_denied"] = exc.errno in (errno.ECONNREFUSED, errno.ENETUNREACH)
    libc = ctypes.CDLL(None, use_errno=True)
    for name, nr, args in (
        ("mount_denied", 165, (b"none", b"/tmp", b"tmpfs", 0, 0)),
        ("remount_denied", 165, (0, b"/tmp", 0, 32, 0)),
        ("umount_denied", 166, (b"/tmp", 2)),
        ("pivot_root_denied", 155, (b"/tmp", b"/tmp")),
        ("unshare_denied", 272, (0x10000000,)),
        ("setns_denied", 308, (-1, 0)),
    ):
        checks[name] = libc.syscall(nr, *args) == -1 and ctypes.get_errno() == errno.EPERM
    for name, passed in checks.items():
        print(f"{name}: {'PASS' if passed else 'FAIL'}", flush=True)
    return 0 if checks and all(checks.values()) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bwrap", type=Path, default=Path("/opt/pico-sandbox/bin/bwrap"))
    parser.add_argument("--claude", type=Path, required=True)
    parser.add_argument("--label", default="pico_worker_bwrap//&pico_worker_child (enforce)")
    parser.add_argument("--child", nargs=3, metavar=("BASE", "UID", "PORT"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        return child(Path(args.child[0]), int(args.child[1]), args.label, int(args.child[2]))
    if sys.platform != "linux" or os.uname().machine != "x86_64" or os.getuid() == 0:
        raise SystemExit("Run as the non-root worker account on Linux x86-64.")
    bwrap, claude = args.bwrap.resolve(strict=True), args.claude.resolve(strict=True)
    validate_bwrap(bwrap)
    if digest(bwrap) != BWRAP_SHA256 or digest(claude) != CLAUDE_SHA256:
        raise SystemExit("Unrecognized binary digest; refusing undocumented helper invocation.")
    with tempfile.TemporaryDirectory(prefix="pico-sandbox-verify-") as directory, socket.socket(socket.AF_INET) as listener:
        base = Path(directory)
        (base / "workspace").mkdir()
        (base / "protected").mkdir()
        (base / "secret").write_text("secret-value")
        assert (base / "secret").read_text() == "secret-value"
        (base / "protected" / "host-write").write_text("host control")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM):
            pass
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
        listener.accept()[0].close()
        env = {"PATH": "/usr/bin:/bin"}
        outer = [str(bwrap), "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                 "--unshare-user", "--unshare-pid", "--unshare-net", "--die-with-parent"]
        def run(command: list[str]) -> int:
            return subprocess.run(["/usr/bin/setpriv", "--no-new-privs", *command], env=env, timeout=20).returncode
        if run([*outer, "/usr/bin/unshare", "--user", "--map-root-user", "true"]) != 0:
            raise SystemExit("Nested namespace preflight failed.")
        print("nested_namespace: PASS", flush=True)
        command = [*outer, "--cap-drop", "ALL", "--chdir", str(base / "workspace"),
                   "--bind", str(base / "workspace"), str(base / "workspace"),
                   "--ro-bind", "/dev/null", str(base / "secret"), "/usr/bin/env", "ARGV0=apply-seccomp",
                   str(claude), "/usr/bin/python3", str(Path(__file__).resolve()), "--claude", str(claude),
                   "--label", args.label, "--child", str(base), str(os.getuid()), str(port)]
        return run(command)


if __name__ == "__main__":
    raise SystemExit(main())
