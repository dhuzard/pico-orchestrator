"""Offline safety checks for the opt-in, no-model sandbox diagnostic."""
import importlib.util
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("sandbox_candidate", ROOT / "tools/verify_claude_sandbox_candidate.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SandboxCandidateTest(TestCase):
    def invoke(self, statuses, digests):
        calls = []
        directories = []
        tempdir = tempfile.TemporaryDirectory

        def record_tempdir(*args, **kwargs):
            result = tempdir(*args, **kwargs)
            directories.append(Path(result.name))
            return result

        def fake_run(*args, **kwargs):
            calls.append(args[0])
            return SimpleNamespace(returncode=statuses[len(calls) - 1])

        argv = ["verify", "--bwrap", "/bin/true", "--claude", "/bin/true"]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(module.os, "getuid", return_value=1001), \
                mock.patch.object(module, "digest", side_effect=digests), \
                mock.patch.object(module.tempfile, "TemporaryDirectory", side_effect=record_tempdir), \
                mock.patch.object(module.subprocess, "run", side_effect=fake_run):
            try:
                result = module.main()
            except SystemExit as exc:
                result = str(exc)
        return result, calls, directories

    def test_unknown_executable_never_runs_a_command(self):
        result, calls, dirs = self.invoke([], ["unexpected"])
        self.assertIn("Unrecognized binary digest", result)
        self.assertEqual(calls, [])
        self.assertEqual(dirs, [])

    def test_failed_preflight_stops_before_helper_and_cleans_tempdir(self):
        result, calls, dirs = self.invoke([1], [module.BWRAP_SHA256, module.CLAUDE_SHA256])
        self.assertIn("preflight failed", result)
        self.assertEqual(len(calls), 1)
        self.assertTrue(dirs and all(not path.exists() for path in dirs))

    def test_failed_helper_propagates_and_cleans_tempdir(self):
        result, calls, dirs = self.invoke([0, 19], [module.BWRAP_SHA256, module.CLAUDE_SHA256])
        self.assertEqual(result, 19)
        self.assertEqual(len(calls), 2)
        self.assertTrue(dirs and all(not path.exists() for path in dirs))

    def test_rejects_unsafe_bwrap_ownership_mode_and_capabilities(self):
        import errno
        import stat
        safe_dir = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
        safe_file = SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=0)
        target = Path('/opt/pico-sandbox/bin/bwrap')
        for file_info in (SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=1001),
                          SimpleNamespace(st_mode=stat.S_IFREG | 0o775, st_uid=0),
                          SimpleNamespace(st_mode=stat.S_IFREG | 0o4755, st_uid=0)):
            with self.subTest(mode=file_info.st_mode, uid=file_info.st_uid), \
                    mock.patch.object(Path, 'stat', return_value=file_info):
                with self.assertRaises(ValueError):
                    module.validate_bwrap(target)
        def stat_path(path):
            return safe_file if path == target else safe_dir
        with mock.patch.object(Path, 'stat', stat_path), mock.patch.object(module.os, 'getxattr', return_value=b'cap'):
            with self.assertRaisesRegex(ValueError, 'no file capabilities'):
                module.validate_bwrap(target)
        bad_dir = SimpleNamespace(st_mode=stat.S_IFDIR | 0o777, st_uid=0)
        def unsafe_parent(path):
            return safe_file if path == target else bad_dir
        with mock.patch.object(Path, 'stat', unsafe_parent):
            with self.assertRaisesRegex(ValueError, 'parent directories'):
                module.validate_bwrap(target)
        with mock.patch.object(Path, 'stat', stat_path), \
                mock.patch.object(module.os, 'getxattr', side_effect=OSError(errno.ENODATA, 'absent')):
            module.validate_bwrap(target)
