"""Parent-checkout snapshot guard: a complete digest or a typed, metadata-only failure; never a partial digest.

The helper runs real Git against disposable repositories. A PATH shim may only delay or fail selected Git
subcommands (it never fabricates output); everything else is the real Git.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import parent_orchestration as po  # noqa: E402
import provenance  # noqa: E402
import session_scope  # noqa: E402
from test_run_lease import StateTestCase  # noqa: E402

REAL_GIT = shutil.which("git")
SHIM = """#!/usr/bin/env bash
args=("$@"); i=0
while [ $i -lt ${#args[@]} ]; do case "${args[$i]}" in -C|-c) i=$((i+2));; -*) i=$((i+1));; *) break;; esac; done
sub="${args[$i]:-}"
if [ -n "${PICO_GIT_SHIM_CONTROL:-}" ] && [ -f "$PICO_GIT_SHIM_CONTROL" ]; then
  while read -r action name value; do
    [ "$name" = "$sub" ] || continue
    case "$action" in delay) sleep "$value";; fail) exit "$value";; esac
  done < "$PICO_GIT_SHIM_CONTROL"
fi
exec "%s" "$@"
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run([REAL_GIT, "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(cwd: Path, message: str, *args: str) -> None:
    git(cwd, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", *args, "-m", message)


def add_submodule(repo: Path, base: Path, name: str = "child") -> Path:
    source = base / f"{name}-source"
    git(repo, "clone", "-q", str(repo), str(source))
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), name)
    commit(repo, f"add {name}")
    return repo / name


class SnapshotFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "tracked.txt").write_text("base\n")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "base")
        shim = self.base / "shim"
        shim.mkdir()
        (shim / "git").write_text(SHIM % REAL_GIT)
        (shim / "git").chmod(0o755)
        self.control = self.base / "control"
        env = unittest.mock.patch.dict(os.environ, {"PATH": f"{shim}{os.pathsep}{os.environ['PATH']}",
                                                    "PICO_GIT_SHIM_CONTROL": str(self.control)})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def shim(self, *lines: str) -> None:
        self.control.write_text("".join(line + "\n" for line in lines))

    def snap(self, **kwargs) -> dict:
        return session_scope.snapshot(str(self.repo), **kwargs)

    def assert_failed(self, result: dict, suboperation: str, error_kind: str) -> None:
        self.assertFalse(result["ok"], result)
        self.assertNotIn("digest", result)
        self.assertEqual((result["suboperation"], result["error_kind"]), (suboperation, error_kind), result)
        self.assertEqual(result["operation"], "snapshot")
        self.assertIsInstance(result["elapsed_ms"], int)
        self.assertIsInstance(result["deadline_ms"], int)
        # Metadata only: no Git output or stderr text in a failure.
        self.assertLessEqual(set(result), {"ok", "blocker_code", "operation", "suboperation", "error_kind", "exit_code",
                                           "signal", "errno", "elapsed_ms", "deadline_ms"})


class SnapshotDigestTest(SnapshotFixture):
    def test_unchanged_checkout_has_a_stable_complete_digest(self):
        first, second = self.snap(), self.snap()
        self.assertTrue(first["ok"], first)
        self.assertRegex(first["digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(first["digest"], second["digest"])
        self.assertEqual((first["head_state"], first["status_count"]), ("commit", 0))
        self.assertEqual(first["deadline_ms"], session_scope.SNAPSHOT_DEFAULT_DEADLINE_MS)

    def test_snapshot_from_subdirectory_uses_the_checkout_root(self):
        nested = self.repo / "nested"
        nested.mkdir()
        (nested / "sample.txt").write_text("sample")
        git(self.repo, "add", "nested/sample.txt")
        expected = self.snap()
        observed = session_scope.snapshot(str(nested))
        self.assertTrue(observed["ok"], observed)
        self.assertEqual(observed["index_count"], expected["index_count"])
        self.assertEqual(observed["digest"], expected["digest"])

    def test_a_tracked_edit_behind_an_unchanged_status_line_changes_the_digest(self):
        (self.repo / "tracked.txt").write_text("one\n")
        before = self.snap()
        (self.repo / "tracked.txt").write_text("two\n")
        after = self.snap()
        self.assertEqual(before["status"], after["status"], "same porcelain line")
        self.assertNotEqual(before["digest"], after["digest"])

    def test_a_staged_only_change_changes_the_digest(self):
        # Worktree and index agree both times ("M "): only the staged blob differs.
        (self.repo / "tracked.txt").write_text("one\n")
        git(self.repo, "add", "tracked.txt")
        before = self.snap()
        (self.repo / "tracked.txt").write_text("two\n")
        git(self.repo, "add", "tracked.txt")
        after = self.snap()
        self.assertEqual(before["status"], after["status"])
        self.assertNotEqual(before["digest"], after["digest"])

    def test_an_index_only_flag_changes_the_digest(self):
        before = self.snap()
        git(self.repo, "update-index", "--assume-unchanged", "tracked.txt")
        after = self.snap()
        self.assertEqual(before["status"], after["status"])
        self.assertNotEqual(before["digest"], after["digest"], "the index itself is part of the digest")

    def test_untracked_files_are_hashed_nul_safely(self):
        odd = "new\tline\nname.txt"
        (self.repo / odd).write_text("one\n")
        before = self.snap()
        self.assertEqual((before["untracked_count"], before["status"]), (1, [f"?? {odd}"]))
        (self.repo / odd).write_text("two\n")
        self.assertNotEqual(self.snap()["digest"], before["digest"])
        os.chmod(self.repo / odd, 0o755)
        self.assertNotEqual(self.snap()["digest"], before["digest"], "the executable bit is recorded")

    def test_rename_records_parse_as_one_entry(self):
        git(self.repo, "mv", "tracked.txt", "moved.txt")
        result = self.snap()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], ["R  moved.txt"])

    def test_untracked_nested_repository_is_recorded_by_name(self):
        nested = self.repo / "nested"
        nested.mkdir()
        git(nested, "init", "-q")
        (nested / "inside.txt").write_text("x\n")
        before = self.snap()
        self.assertTrue(before["ok"], before)
        (nested / "inside.txt").write_text("y\n")
        self.assertEqual(self.snap()["digest"], before["digest"], "its contents belong to that repository")

    def test_unborn_head_is_distinguished_from_a_broken_head(self):
        unborn = self.base / "unborn"
        unborn.mkdir()
        git(unborn, "init", "-q", "-b", "main")
        result = session_scope.snapshot(str(unborn))
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["head"], result["head_state"]), (None, "unborn"))
        self.assertEqual(session_scope.head(str(unborn)), {"ok": True, "head": None})
        # A branch ref that names a missing object is broken, not unborn.
        (unborn / ".git/refs/heads/main").write_text("0" * 39 + "1\n")
        broken = session_scope.snapshot(str(unborn))
        self.assert_failed(broken, "head_ref", "NonzeroExit")
        self.assertEqual(broken["exit_code"], 128)
        self.assertFalse(session_scope.head(str(unborn))["ok"])

    def test_outside_a_repository_is_unresolved(self):
        outside = self.base / "outside"
        outside.mkdir()
        result = session_scope.snapshot(str(outside))
        self.assert_failed(result, "repository", "NonzeroExit")
        self.assertEqual(result["blocker_code"], "SESSION_TARGET_UNRESOLVED")

    def test_inherited_repository_environment_is_ignored(self):
        other = self.base / "other"
        other.mkdir()
        git(other, "init", "-q")
        expected = self.snap()["digest"]
        with unittest.mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git"), "GIT_INDEX_FILE": str(other / "x")}):
            self.assertEqual(self.snap()["digest"], expected)
        self.assertEqual(session_scope.git_env()["GIT_OPTIONAL_LOCKS"], "0")

    def test_snapshot_never_rewrites_the_index(self):
        index = self.repo / ".git/index"
        os.utime(self.repo / "tracked.txt", (time.time() + 5, time.time() + 5))
        stamp = index.stat().st_mtime_ns
        self.assertTrue(self.snap()["ok"])
        self.assertEqual(index.stat().st_mtime_ns, stamp, "GIT_OPTIONAL_LOCKS=0: no opportunistic index refresh")


class SnapshotFailureTest(SnapshotFixture):
    def test_a_failed_git_command_is_a_failure_not_an_incomplete_digest(self):
        for command, suboperation in (("ls-files", "index"), ("status", "status"), ("rev-parse", "repository")):
            with self.subTest(command=command):
                self.shim(f"fail {command} 128")
                result = self.snap()
                self.assert_failed(result, suboperation, "NonzeroExit")
                self.assertEqual(result["exit_code"], 128)

    def test_unterminated_records_fail_validation(self):
        for parser, data in ((session_scope._status_entries, b"?? a"),
                             (session_scope._index_entries, b"H 100644 " + b"0" * 40 + b" 0\ta")):
            with self.assertRaises(session_scope.SnapshotFailure) as caught:
                parser(data)
            self.assertEqual(caught.exception.error_kind, "MalformedOutput")

    def test_dirty_submodule_requires_a_complete_snapshot(self):
        add_submodule(self.repo, self.base)
        self.assertTrue(self.snap()["ok"])
        (self.repo / "child" / "tracked.txt").write_text("local changes")
        # Dirt that predates the worker fails every snapshot (so every worker call is refused) until it is cleaned.
        for _ in range(3):
            self.assert_failed(self.snap(), "worktree_hash", "TrackedDirectoryUnverifiable")
        git(self.repo / "child", "checkout", "--", "tracked.txt")
        self.assertTrue(self.snap()["ok"])

    def test_malformed_git_output_is_a_failure(self):
        # Exit 0 without output: `rev-parse --show-toplevel` printed nothing.
        self.shim("fail rev-parse 0")
        self.assert_failed(self.snap(), "repository", "MalformedOutput")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads any file")
    def test_an_unreadable_file_is_a_failure(self):
        (self.repo / "secret.txt").write_text("x\n")
        (self.repo / "tracked.txt").write_text("changed\n")
        for name, suboperation in (("secret.txt", "untracked_hash"), ("tracked.txt", "worktree_hash")):
            with self.subTest(name=name):
                os.chmod(self.repo / name, 0)
                try:
                    result = self.snap()
                finally:
                    os.chmod(self.repo / name, 0o644)
                self.assert_failed(result, suboperation, "OSError")
                self.assertEqual(result["errno"], 13)

    def test_a_failed_unborn_head_check_is_not_unborn(self):
        unborn = self.base / "unborn"
        unborn.mkdir()
        git(unborn, "init", "-q", "-b", "main")
        for command, suboperation in (("symbolic-ref", "head_symbolic"), ("show-ref", "head_ref")):
            with self.subTest(command=command):
                self.shim(f"fail {command} 128")
                result = session_scope.snapshot(str(unborn))
                self.assert_failed(result, suboperation, "NonzeroExit")
                self.assertFalse(session_scope.head(str(unborn))["ok"], "a failure is never reported as no commit")

    def test_a_slow_command_is_reported_within_the_deadline(self):
        self.shim("delay status 20")
        started = time.monotonic()
        result = self.snap(deadline_ms=700)
        elapsed = time.monotonic() - started
        self.assert_failed(result, "status", "Timeout")
        self.assertEqual(result["deadline_ms"], 700)
        self.assertGreaterEqual(result["elapsed_ms"], 600)
        self.assertLess(elapsed, 5, "the delayed Git process group was killed, not waited for")

    def test_a_slow_unchanged_checkout_within_the_deadline_passes(self):
        expected = self.snap()["digest"]
        self.shim("delay status 0.4")
        result = self.snap(deadline_ms=5000)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["digest"], expected)
        self.assertGreaterEqual(result["elapsed_ms"], 400)

    def test_input_is_closed(self):
        for payload in ({"operation": "snapshot", "path": str(self.repo), "deadline_ms": 5},
                        {"operation": "snapshot", "path": str(self.repo), "deadline_ms": "100"},
                        {"operation": "snapshot", "path": str(self.repo), "extra": 1}):
            with self.subTest(payload=payload):
                out = subprocess.run([sys.executable, "-B", str(ROOT / "tools/session_scope.py")], input=json.dumps(payload),
                                     capture_output=True, text=True, check=True)
                self.assertEqual(json.loads(out.stdout), {"ok": False, "blocker_code": "SESSION_SCOPE_INPUT_INVALID"})

    @unittest.skipUnless(os.name == "posix" and Path("/proc").is_dir(), "needs POSIX process groups and /proc")
    def test_sigterm_takes_running_git_down_with_the_helper(self):
        marker = "29.731"
        self.shim(f"delay status {marker}")
        helper = subprocess.Popen([sys.executable, "-B", str(ROOT / "tools/session_scope.py")], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        helper.stdin.write(json.dumps({"operation": "snapshot", "path": str(self.repo)}).encode())
        helper.stdin.close()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not self._sleepers(marker):
            time.sleep(0.05)
        self.assertTrue(self._sleepers(marker), "the delayed Git command is running")
        os.killpg(helper.pid, signal.SIGTERM)
        self.assertEqual(helper.wait(timeout=5), 128 + signal.SIGTERM)
        self.assertEqual(helper.stdout.read(), b"", "no verdict after termination")
        helper.stdout.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self._sleepers(marker):
            time.sleep(0.05)
        self.assertEqual(self._sleepers(marker), [], "no Git descendant outlives the helper")

    @staticmethod
    def _sleepers(marker: str) -> list[int]:
        found = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().split(b"\0")
                state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
            except OSError:
                continue
            if b"sleep" in cmdline[0] and marker.encode() in cmdline and state != "Z":
                found.append(int(entry.name))
        return found


class SnapshotExecutionIsolationTest(SnapshotFixture):
    """No program named by inherited or repository-local configuration runs during a snapshot.

    Every case first proves, with the real Git in the same configuration (GIT_OPTIONAL_LOCKS=0, like the helper),
    that the canary does execute; only then does it assert that the snapshot never starts it.
    """

    def canary(self, name: str = "canary") -> tuple[Path, Path]:
        marker = self.base / f"{name}.ran"
        script = self.base / f"{name}.sh"
        script.write_text(f"#!/bin/sh\necho ran >> '{marker}'\nexec cat\n")
        script.chmod(0o755)
        return script, marker

    def assert_canary_contained(self, marker: Path, cwd: Path | None = None) -> dict:
        control = subprocess.run([REAL_GIT, "-C", str(cwd or self.repo), "status", "--porcelain=v1", "-z",
                                  "--untracked-files=all", "--ignore-submodules=none"], stdin=subprocess.DEVNULL,
                                 capture_output=True, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        self.assertTrue(marker.exists(), f"control: plain git status runs the canary (exit {control.returncode})")
        marker.unlink()
        result = self.snap()
        self.assertFalse(marker.exists(), "the snapshot executed a configured program")
        self.assertTrue(session_scope.head(str(self.repo))["ok"])
        self.assertFalse(marker.exists(), "the head check executed a configured program")
        return result

    def test_inherited_configuration_cannot_run_fsmonitor(self):
        script, marker = self.canary()
        config_file = self.base / "injected.gitconfig"
        config_file.write_text(f"[core]\n\tfsmonitor = {script}\n")
        home = self.base / "home"
        (home / ".config" / "git").mkdir(parents=True)
        (home / ".gitconfig").write_text(config_file.read_text())
        clean = self.snap()["digest"]
        vectors = {
            "count": {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": str(script)},
            "parameters": {"GIT_CONFIG_PARAMETERS": f"'core.fsmonitor'='{script}'"},
            "global": {"GIT_CONFIG_GLOBAL": str(config_file)},
            "system": {"GIT_CONFIG_SYSTEM": str(config_file)},
            "home": {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")},
        }
        for name, variables in vectors.items():
            with self.subTest(vector=name), unittest.mock.patch.dict(os.environ, variables):
                for unset in ("GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL") if name in ("system", "home") else ():
                    os.environ.pop(unset, None)
                result = self.assert_canary_contained(marker)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["digest"], clean, "inherited configuration does not change what is read")

    def test_repository_local_fsmonitor_and_clean_filter_never_run(self):
        script, marker = self.canary()
        with self.subTest(integration="fsmonitor"):
            git(self.repo, "config", "core.fsmonitor", str(script))
            self.assertTrue(self.assert_canary_contained(marker)["ok"])
            git(self.repo, "config", "--unset", "core.fsmonitor")
        with self.subTest(integration="clean filter"):
            git(self.repo, "config", "filter.canary.clean", str(script))
            git(self.repo, "config", "filter.canary.required", "true")
            (self.repo / ".git" / "info" / "attributes").write_text("*.txt filter=canary\n")
            # A stale stat makes status re-hash the clean file, which is where Git applies the clean filter.
            future = time.time() + 60
            os.utime(self.repo / "tracked.txt", (future, future))
            self.assertTrue(self.assert_canary_contained(marker)["ok"])
        with self.subTest(integration="long-running process filter"):
            git(self.repo, "config", "--unset", "filter.canary.clean")
            git(self.repo, "config", "filter.canary.process", str(script))
            future = time.time() + 120
            os.utime(self.repo / "tracked.txt", (future, future))
            self.assertTrue(self.assert_canary_contained(marker)["ok"])

    def test_submodule_recursion_runs_no_configured_program(self):
        child = add_submodule(self.repo, self.base)
        script, marker = self.canary()
        child_git = Path(git(child, "rev-parse", "--absolute-git-dir"))
        with self.subTest(integration="submodule fsmonitor"):
            git(child, "config", "core.fsmonitor", str(script))
            self.assertTrue(self.assert_canary_contained(marker)["ok"])
            git(child, "config", "--unset", "core.fsmonitor")
        with self.subTest(integration="submodule clean filter"):
            git(child, "config", "filter.childcanary.clean", str(script))
            (child_git / "info").mkdir(exist_ok=True)
            (child_git / "info" / "attributes").write_text("*.txt filter=childcanary\n")
            future = time.time() + 60
            os.utime(child / "tracked.txt", (future, future))
            self.assertTrue(self.assert_canary_contained(marker)["ok"])
            git(child, "config", "--unset", "filter.childcanary.clean")
        with self.subTest(integration="inherited GIT_EXEC_PATH"):
            exec_dir = self.base / "exec"
            exec_dir.mkdir()
            (exec_dir / "git").write_text(f"#!/bin/sh\necho ran >> '{marker}'\nexec '{REAL_GIT}' \"$@\"\n")
            (exec_dir / "git").chmod(0o755)
            with unittest.mock.patch.dict(os.environ, {"GIT_EXEC_PATH": str(exec_dir)}):
                self.assertTrue(self.assert_canary_contained(marker)["ok"])

    def test_every_snapshot_command_runs_with_overridden_integrations(self):
        with unittest.mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.fsmonitor",
                                                   "GIT_CONFIG_VALUE_0": "x", "GIT_TRACE": "1", "GIT_EXEC_PATH": "/x"}):
            env = session_scope.snapshot_git_env(("a.b",))
        self.assertFalse({"GIT_TRACE", "GIT_EXEC_PATH", "GIT_CONFIG_PARAMETERS"} & set(env))
        self.assertEqual((env["GIT_CONFIG_NOSYSTEM"], env["GIT_CONFIG_GLOBAL"], env["GIT_OPTIONAL_LOCKS"]),
                         ("1", os.devnull, "0"))
        config = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
        self.assertEqual(config["core.fsmonitor"], "false")
        self.assertEqual(config["core.hooksPath"], os.devnull)
        self.assertEqual((config["filter.a.b.clean"], config["filter.a.b.process"], config["filter.a.b.required"]),
                         ("", "", "false"))


class SnapshotTrackedModeTest(SnapshotFixture):
    """Tracked-path type and permission bits are covered by lstat alone, also where Git ignores them."""

    def test_tracked_chmod_with_filemode_false_changes_the_digest(self):
        git(self.repo, "config", "core.fileMode", "false")
        before = self.snap()
        os.chmod(self.repo / "tracked.txt", 0o755)
        after = self.snap()
        self.assertTrue(after["ok"], after)
        self.assertEqual(before["status"], after["status"], "Git itself reports nothing")
        self.assertNotEqual(before["digest"], after["digest"])
        os.chmod(self.repo / "tracked.txt", 0o644)
        self.assertEqual(self.snap()["digest"], before["digest"])

    def test_clean_tracked_files_are_not_reread(self):
        for index in range(25):
            (self.repo / f"file{index}.txt").write_text(f"{index}\n")
        git(self.repo, "add", ".")
        commit(self.repo, "many")
        git(self.repo, "config", "core.fileMode", "false")
        opened: list[str] = []
        real_open = os.open

        def counting_open(path, *args, **kwargs):
            if str(self.repo) in os.fsdecode(path):
                opened.append(os.fsdecode(path))
            return real_open(path, *args, **kwargs)

        with unittest.mock.patch.object(session_scope.os, "open", counting_open):
            result = self.snap()
        self.assertTrue(result["ok"], result)
        self.assertEqual(opened, [], "clean tracked content is never read")

    def test_tracked_symlinks_are_not_followed(self):
        outside = self.base / "outside.txt"
        outside.write_text("x\n")
        (self.repo / "link").symlink_to(outside)
        git(self.repo, "add", "link")
        commit(self.repo, "link")
        git(self.repo, "config", "core.fileMode", "false")
        before = self.snap()
        os.chmod(outside, 0o700)
        self.assertEqual(self.snap()["digest"], before["digest"], "the link itself did not change")

    def test_conflict_stages_and_gitlinks_are_covered_once(self):
        add_submodule(self.repo, self.base)
        git(self.repo, "checkout", "-q", "-b", "side")
        (self.repo / "tracked.txt").write_text("side\n")
        commit(self.repo, "side", "-a")
        git(self.repo, "checkout", "-q", "main")
        (self.repo / "tracked.txt").write_text("main\n")
        commit(self.repo, "main", "-a")
        subprocess.run([REAL_GIT, "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
                        "merge", "-q", "side"], capture_output=True)
        stages = git(self.repo, "ls-files", "--stage", "tracked.txt").splitlines()
        self.assertEqual(len(stages), 3, "a three-stage conflict")
        git(self.repo, "config", "core.fileMode", "false")
        first, second = self.snap(), self.snap()
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["digest"], second["digest"])
        os.chmod(self.repo / "child", 0o700)
        self.assertNotEqual(self.snap()["digest"], first["digest"], "the gitlink directory is lstat'ed, not entered")

    def test_tracked_modes_respect_the_deadline(self):
        with self.assertRaises(session_scope.SnapshotFailure) as caught:
            session_scope._tracked_modes(os.fsencode(self.repo), [b"tracked.txt"], time.monotonic() - 1)
        self.assertEqual((caught.exception.suboperation, caught.exception.error_kind), ("modes", "DeadlineExceeded"))


class SnapshotRootEnvTest(SnapshotFixture):
    """Root `.env`/`.env.*` entries are covered even when ignored; other ignored paths are explicitly not."""

    def setUp(self):
        super().setUp()
        (self.repo / ".git" / "info" / "exclude").write_text(".env*\nnode_modules/\n.venv/\n.cache/\n")

    def test_ignored_root_env_content_mode_link_and_presence_change_the_digest(self):
        for name in (".env", ".env.local"):
            with self.subTest(name=name):
                path = self.repo / name
                absent = self.snap()
                path.write_text("TOKEN=one\n")
                one = self.snap()
                self.assertEqual(one["status"], absent["status"], "ignored: Git reports nothing")
                path.write_text("TOKEN=two\n")
                two = self.snap()
                os.chmod(path, 0o600)
                private = self.snap()
                path.unlink()
                path.symlink_to(self.base / "elsewhere")
                linked = self.snap()
                digests = [absent["digest"], one["digest"], two["digest"], private["digest"], linked["digest"]]
                self.assertEqual(len(set(digests)), len(digests), digests)
                path.unlink()
                self.assertEqual(self.snap()["digest"], absent["digest"])

    def test_a_root_env_directory_fails_closed(self):
        (self.repo / ".env.d").mkdir()
        (self.repo / ".env.d" / "secret").write_text("x\n")
        self.assert_failed(self.snap(), "root_env", "DirectoryUnverifiable")

    def test_other_ignored_paths_are_explicitly_not_covered(self):
        for relative in ("node_modules/pkg/index.js", ".venv/lib/site.py", ".cache/blob", "nested/.env", ".envrc"):
            (self.repo / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / relative).write_text("one\n")
        (self.repo / ".git" / "info" / "exclude").write_text(".env*\nnode_modules/\n.venv/\n.cache/\nnested/\n")
        before = self.snap()
        for relative in ("node_modules/pkg/index.js", ".venv/lib/site.py", ".cache/blob", "nested/.env", ".envrc"):
            (self.repo / relative).write_text("two\n")
        self.assertEqual(self.snap()["digest"], before["digest"], "outside the documented coverage")


class SnapshotHeadIdentityTest(SnapshotFixture):
    """The symbolic HEAD (branch name, or detached) is part of the digest, not only the commit it resolves to."""

    def test_switching_between_branches_at_the_same_commit_changes_the_digest(self):
        git(self.repo, "branch", "other")
        on_main = self.snap()
        git(self.repo, "checkout", "-q", "other")
        on_other = self.snap()
        self.assertEqual((on_main["head"], on_main["status"]), (on_other["head"], on_other["status"]))
        self.assertNotEqual(on_main["digest"], on_other["digest"])

    def test_detaching_at_the_same_commit_changes_the_digest(self):
        attached = self.snap()
        git(self.repo, "checkout", "-q", "--detach")
        detached = self.snap()
        self.assertEqual((detached["head"], detached["head_state"]), (attached["head"], "commit"))
        self.assertNotEqual(attached["digest"], detached["digest"])

    def test_renaming_an_unborn_branch_changes_the_digest(self):
        unborn = self.base / "unborn"
        unborn.mkdir()
        git(unborn, "init", "-q", "-b", "main")
        before = session_scope.snapshot(str(unborn))
        git(unborn, "symbolic-ref", "HEAD", "refs/heads/other")
        after = session_scope.snapshot(str(unborn))
        self.assertEqual((after["head"], after["head_state"]), (None, "unborn"))
        self.assertNotEqual(before["digest"], after["digest"])
        self.assertEqual(session_scope.head(str(unborn)), {"ok": True, "head": None}, "head() result is unchanged")


class ScopeCheckRecordTest(StateTestCase):
    def setUp(self):
        super().setUp()
        self.owner = "owner-a"
        self.run_id = self.start_run(self.owner)
        self.schema = json.loads((ROOT / "schemas/run-record.schema.json").read_text())

    def event(self, payload: dict) -> dict:
        return po.record_event(self.run_id, payload, owner_id=self.owner, repo_root=self.records)

    def check(self, **fields) -> dict:
        event = {"kind": "scope_check", "stage": "after", "verdict": "unverifiable", "latched": True, "phase": "t1",
                 "task_id": "t1", "step_id": "codex_reviewer", "tool": "spawn_subagent", "before_digest": "a" * 64,
                 "after_digest": None, "changed_count": None, "operation": "snapshot", "suboperation": "status",
                 "error_kind": "Timeout", "exit_code": None, "signal": None, "elapsed_ms": 24012,
                 "deadline_ms": 24000, "timeout_ms": 30000}
        event.update(fields)
        return event

    def test_a_latched_check_refuses_completed_and_survives_status(self):
        self.event(self.check())
        parent = self.parent(self.run_id)
        self.assertEqual(parent["scope_checks"][0]["code"], "WORKER_SCOPE_UNVERIFIABLE")
        self.assertEqual(po.status(self.run_id, repo_root=self.records)["scope_latch"], "WORKER_SCOPE_UNVERIFIABLE")
        self.assertEqual(provenance.validate_run_record(provenance.get_run(self.records, self.run_id), self.schema), [])
        with self.assertRaises(po.ParentRunError) as refused:
            po.finish(self.run_id, "COMPLETED", owner_id=self.owner, repo_root=self.records)
        self.assertEqual(refused.exception.error_code, "WORKER_SCOPE_UNVERIFIABLE")
        finished = po.finish(self.run_id, "PARTIAL", owner_id=self.owner, repo_root=self.records)
        self.assertEqual(finished["parent_orchestration"]["final_status"], "PARTIAL")

    def test_drift_needs_two_different_complete_digests(self):
        self.event(self.check(verdict="drift", after_digest="b" * 64, changed_count=1, suboperation=None, error_kind=None))
        self.assertEqual(po.status(self.run_id, repo_root=self.records)["scope_latch"], "WORKER_SCOPE_VIOLATION")
        for bad in (self.check(verdict="drift", after_digest=None, error_kind=None),
                    self.check(verdict="drift", after_digest="a" * 64, error_kind=None),
                    self.check(after_digest="b" * 64),
                    self.check(error_kind=None),
                    self.check(before_digest=None),
                    self.check(stage="before"),
                    self.check(suboperation="status: fatal output"),
                    {**self.check(), "stderr": "x"}):
            with self.subTest(bad=bad), self.assertRaises(po.ParentRunError):
                self.event(bad)

    def test_a_refused_call_before_a_worker_does_not_latch(self):
        self.event(self.check(stage="before", latched=False, before_digest=None))
        self.assertIsNone(po.status(self.run_id, repo_root=self.records)["scope_latch"])
        self.assertEqual(po.finish(self.run_id, "COMPLETED", owner_id=self.owner,
                                   repo_root=self.records)["parent_orchestration"]["final_status"], "COMPLETED")

    def test_trimming_keeps_the_first_latched_check(self):
        self.event(self.check(stage="before", latched=False, before_digest=None))
        self.event(self.check())
        for _ in range(po.MAX_SCOPE_CHECKS + 5):
            self.event(self.check(stage="before", latched=False, before_digest=None))
        checks = self.parent(self.run_id)["scope_checks"]
        self.assertEqual(len(checks), po.MAX_SCOPE_CHECKS)
        self.assertEqual(po.scope_latch(self.parent(self.run_id)), "WORKER_SCOPE_UNVERIFIABLE")


if __name__ == "__main__":
    unittest.main()
