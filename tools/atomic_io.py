#!/usr/bin/env python3
"""Atomic, symlink-aware file replacement shared by PICO's state writers.

Every write goes to a fresh temp file created with ``mkstemp`` next to the
destination (so a pre-planted file or symlink at a predictable temp name is
never followed), is flushed and fsynced, and then replaces the destination
with ``os.replace``. The containing directory is fsynced on POSIX on a
best-effort basis. The temp file is removed on any failure.

With ``refuse_symlinks`` (the default) the write fails closed when the
destination itself, or any existing directory between it and ``root``, is a
symbolic link. ``root`` itself is trusted and not inspected, so a symlinked
home or temp directory above it is fine.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

DEFAULT_MODE = 0o666  # like open(): the process umask still applies

_UMASK_LOCK = threading.Lock()


class UnsafePathError(OSError):
    """The destination (or a directory leading to it) is a symlink or escapes its root."""


def _umask() -> int:
    try:
        with open("/proc/self/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except (OSError, ValueError, IndexError):
        pass
    with _UMASK_LOCK:
        current = os.umask(0o022)
        os.umask(current)
    return current


def check_no_symlinks(path: Path, root: Path | None = None) -> None:
    """Raise UnsafePathError if ``path`` or an existing ancestor below ``root`` is a symlink."""
    path = Path(os.path.abspath(path))
    candidates = [path]
    if root is not None:
        root = Path(os.path.abspath(root))
        try:
            relative = path.relative_to(root)
        except ValueError:
            raise UnsafePathError(f"{path} is outside {root}") from None
        current = root
        for part in relative.parts[:-1]:
            current = current / part
            candidates.append(current)
    for candidate in candidates:
        try:
            if stat.S_ISLNK(os.lstat(candidate).st_mode):
                raise UnsafePathError(f"refusing to write through symlink {candidate}")
        except FileNotFoundError:
            continue


def _fsync_directory(directory: Path) -> None:
    if os.name != "posix":
        return
    try:
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path: Path | str, text: str, *, refuse_symlinks: bool = True, root: Path | None = None,
                      mode: int | None = DEFAULT_MODE, encoding: str = "utf-8",
                      validate: Callable[[Path], Any] | None = None) -> None:
    """Atomically replace ``path`` with ``text``.

    The parent directory must already exist. An existing destination keeps its
    permission bits; a new one gets ``mode`` minus the umask (``None`` keeps the
    private 0600 that mkstemp creates). ``validate`` is called with the fully
    written temp path before the replacement; if it raises, the destination is
    left untouched.
    """
    path = Path(path)
    if refuse_symlinks:
        check_no_symlinks(path, root)
    try:
        existing = os.stat(path, follow_symlinks=False)
        target_mode = stat.S_IMODE(existing.st_mode) if stat.S_ISREG(existing.st_mode) else None
    except FileNotFoundError:
        target_mode = None if mode is None else mode & ~_umask()
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            if target_mode is not None:
                if hasattr(os, "fchmod"):
                    os.fchmod(handle.fileno(), target_mode)
                else:  # pragma: no cover - non-POSIX
                    os.chmod(tmp, target_mode)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if validate is not None:
            validate(tmp)
        if refuse_symlinks:
            check_no_symlinks(path, root)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    _fsync_directory(path.parent)


def atomic_write_json(path: Path | str, obj: Any, *, refuse_symlinks: bool = True, root: Path | None = None,
                      mode: int | None = DEFAULT_MODE, trailing_newline: bool = False, **json_kwargs: Any) -> None:
    """Serialize ``obj`` with ``json.dumps(**json_kwargs)`` and write it atomically."""
    text = json.dumps(obj, **json_kwargs)
    if trailing_newline:
        text += "\n"
    atomic_write_text(path, text, refuse_symlinks=refuse_symlinks, root=root, mode=mode)
