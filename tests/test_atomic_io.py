#!/usr/bin/env python3
"""The shared atomic writer: replacement, modes, temp cleanup and symlink refusal."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import atomic_io  # noqa: E402


@unittest.skipUnless(os.name == "posix", "POSIX file modes and symlinks")
class AtomicIoTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def mode(self, path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)

    def test_writes_and_leaves_no_temp_files(self):
        path = self.base / "record.json"
        atomic_io.atomic_write_json(path, {"b": 1, "a": 2}, indent=2, sort_keys=True)
        self.assertEqual(json.loads(path.read_text()), {"a": 2, "b": 1})
        atomic_io.atomic_write_text(path, "second\n")
        self.assertEqual(path.read_text(), "second\n")
        self.assertEqual([p.name for p in self.base.iterdir()], ["record.json"])

    def test_new_file_respects_umask_and_existing_mode_is_kept(self):
        previous = os.umask(0o022)
        try:
            path = self.base / "new.txt"
            atomic_io.atomic_write_text(path, "x")
            self.assertEqual(self.mode(path), 0o644)
            path.chmod(0o640)
            atomic_io.atomic_write_text(path, "y")
            self.assertEqual(self.mode(path), 0o640)
            private = self.base / "private.txt"
            atomic_io.atomic_write_text(private, "z", mode=None)
            self.assertEqual(self.mode(private), 0o600)
        finally:
            os.umask(previous)

    def test_failed_validation_keeps_destination_and_removes_temp(self):
        path = self.base / "config.yaml"
        path.write_text("old\n")

        def reject(temp: Path) -> None:
            self.assertEqual(temp.read_text(), "new\n")
            raise ValueError("invalid")

        with self.assertRaises(ValueError):
            atomic_io.atomic_write_text(path, "new\n", validate=reject)
        self.assertEqual(path.read_text(), "old\n")
        self.assertEqual([p.name for p in self.base.iterdir()], ["config.yaml"])

    def test_refuses_symlinked_destination_and_ancestors(self):
        victim = self.base / "victim.txt"
        victim.write_text("untouched")
        link = self.base / "link.txt"
        link.symlink_to(victim)
        with self.assertRaises(atomic_io.UnsafePathError):
            atomic_io.atomic_write_text(link, "pwned")
        root = self.base / "checkout"
        root.mkdir()
        outside = self.base / "outside"
        outside.mkdir()
        (root / ".pi").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(atomic_io.UnsafePathError):
            atomic_io.atomic_write_text(root / ".pi" / "autonomy.yaml", "pwned", root=root)
        with self.assertRaises(atomic_io.UnsafePathError):
            atomic_io.atomic_write_text(self.base / "elsewhere.txt", "x", root=root)
        self.assertEqual(victim.read_text(), "untouched")
        self.assertEqual(list(outside.iterdir()), [])
        # Without refusal the symlink itself is replaced, never followed.
        atomic_io.atomic_write_text(link, "replaced", refuse_symlinks=False)
        self.assertFalse(link.is_symlink())
        self.assertEqual(victim.read_text(), "untouched")

    def test_symlinked_root_is_trusted(self):
        real = self.base / "real"
        (real / "sub").mkdir(parents=True)
        alias = self.base / "alias"
        alias.symlink_to(real, target_is_directory=True)
        atomic_io.atomic_write_text(alias / "sub" / "file.txt", "ok", root=alias)
        self.assertEqual((real / "sub" / "file.txt").read_text(), "ok")


if __name__ == "__main__":
    unittest.main()
