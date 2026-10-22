#!/usr/bin/env python3
"""Deterministic tests for the headless single-repository execution interface.

`pi` is a fake on PATH that behaves like the lifecycle extension would in print
mode: it creates (or restores) the parent run through tools/parent_orchestration.py,
stores the run id in a session entry, and then finishes, settles, crashes, or hangs
according to FAKE_PI_BEHAVIOR.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import headless_run as hr  # noqa: E402
import provenance  # noqa: E402
import run_lease  # noqa: E402
import session_scope  # noqa: E402

FAKE_PI = r'''#!/usr/bin/env python3
import json, os, subprocess, sys, time, glob
args = sys.argv[1:]
sid = args[args.index("--session-id") + 1]
sdir = args[args.index("--session-dir") + 1]
prompt = args[args.index("--") + 1]
assert ("PI_ORCHESTRATION_RUN_V1" in prompt) or glob.glob(os.path.join(sdir, f"*_{sid}.jsonl")), \
    "a new session needs the orchestration marker"
assert "--print" in args and "--mode" in args
tools = os.environ["FAKE_PI_TOOLS"]
behavior = os.environ.get("FAKE_PI_BEHAVIOR_" + os.path.basename(os.getcwd()), os.environ.get("FAKE_PI_BEHAVIOR", "finish"))
log = os.environ.get("FAKE_PI_LOG")
def note(event):
    if log:
        with open(log, "a") as f:
            f.write(f"{time.monotonic():.6f} {event} {os.path.basename(os.getcwd())}\n")
parent = os.path.join(tools, "parent_orchestration.py")
def pcmd(*a):
    out = subprocess.run([sys.executable, parent, *a], capture_output=True, text=True)
    return json.loads(out.stdout)
existing = glob.glob(os.path.join(sdir, f"*_{sid}.jsonl"))
run_id = None
# Like the lifecycle: read the launcher's owner once, never pass it on, hand off explicitly.
owner = os.environ.pop("PI_ORCHESTRATION_LEASE_OWNER", None) or f"fake-{os.getpid()}"
handoff = ["--handoff"] if owner.startswith("headless-") else []
marker = "PI_ORCHESTRATION_RUN_V1" in prompt
if existing and marker:
    # Real lifecycle: before_agent_start settles the restored run and starts anew.
    for line in open(existing[0]):
        e = json.loads(line)
        if e.get("type") == "custom" and e.get("customType") == "pi-orchestration-parent-v1":
            # Restore (lease + checkpoint), then settle as the restored run's owner.
            pcmd("resume", e["data"]["run_id"], "--worktree", os.getcwd(), "--repo", os.environ["PI_TARGET_REPOSITORY"],
                 "--owner", owner, "--pid", str(os.getpid()), *handoff)
            pcmd("settle", e["data"]["run_id"], "--owner", owner)
    existing = []
    os.remove(glob.glob(os.path.join(sdir, f"*_{sid}.jsonl"))[0])
if existing:
    for line in open(existing[0]):
        e = json.loads(line)
        if e.get("type") == "custom" and e.get("customType") == "pi-orchestration-parent-v1":
            run_id = e["data"]["run_id"]
    # Real lifecycle restore: verify identity, take the run lease, verify the resume checkpoint.
    st = pcmd("resume", run_id, "--worktree", os.getcwd(), "--repo", os.environ["PI_TARGET_REPOSITORY"],
              "--owner", owner, "--pid", str(os.getpid()), *handoff)
    if st.get("status") != "IN_PROGRESS":
        print(json.dumps({"type": "fake", "run_id": run_id, "resume_refused": st}))
        sys.exit(4)
    session_file = existing[0]
else:
    started = pcmd("start", "--worktree", os.getcwd(), "--repo", os.environ["PI_TARGET_REPOSITORY"],
                   "--owner", owner, "--pid", str(os.getpid()))
    run_id = started["run_id"]
    session_file = os.path.join(sdir, f"20261019T000000_{sid}.jsonl")
    with open(session_file, "a") as f:
        f.write(json.dumps({"type": "custom", "customType": "pi-orchestration-parent-v1",
                            "data": {"run_id": run_id, "session_id": sid, "target_cwd": os.getcwd(), "active": True}}) + "\n")
print(json.dumps({"type": "fake", "run_id": run_id, "behavior": behavior}))
sys.stdout.flush()
note("start")
if os.environ.get("FAKE_PI_MESS") == "parent-apply":
    # What a real lead did in the #4 pilot: `git diff ... | git apply` in the parent checkout itself (#68).
    with open("README.md", "a") as tracked:
        tracked.write("applied in the parent checkout\n")
elif os.environ.get("FAKE_PI_MESS") == "parent-new-file":
    # The same, writing a new file: untracked, so reported and never refused.
    os.makedirs(os.path.join(".pi", "worktrees", "own"), exist_ok=True)
    open(os.path.join(".pi", "worktrees", "own", "f.txt"), "w").write("PICO's own worktree area\n")
    os.makedirs("generated", exist_ok=True)
    open(os.path.join("generated", "slug.py"), "w").write("new file outside any worktree\n")
elif os.environ.get("FAKE_PI_MESS"):
    # What real leads did: switch the parent checkout to a batch branch and leave a worker worktree.
    subprocess.run(["git", "switch", "-q", "-c", "pico/batch-1"], capture_output=True)
    subprocess.run(["git", "worktree", "add", "-q", "-b", "pico/task-1", os.path.join(sdir, "..", "wt-task-1")], capture_output=True)
    if os.environ["FAKE_PI_MESS"] == "dirty":
        open(os.path.join(sdir, "..", "wt-task-1", "scratch.txt"), "w").write("uncommitted\n")
here = os.path.basename(os.getcwd())
usage = os.environ.get("FAKE_PI_USAGE_" + here, os.environ.get("FAKE_PI_USAGE"))  # "<input>,<output>[,<cost>]": report worker usage like a real child
if usage:
    numbers = usage.split(",")
    event = {"kind": "usage", "backend": "claude-code", "input_tokens": int(numbers[0]), "output_tokens": int(numbers[1]),
             "total_tokens": int(numbers[0]) + int(numbers[1])}
    if len(numbers) > 2:
        event["estimated_cost_usd"] = float(numbers[2])
    subprocess.run([sys.executable, parent, "event", run_id, "--owner", owner], input=json.dumps(event),
                   capture_output=True, text=True)
if behavior == "slow":
    time.sleep(float(os.environ.get("FAKE_PI_SLEEP_" + here, os.environ.get("FAKE_PI_SLEEP", "0.4"))))
    behavior = "finish"
if behavior == "finish":
    pcmd("finish", run_id, "PARTIAL", "--owner", owner)
elif behavior == "settle":
    pcmd("settle", run_id, "--owner", owner)
elif behavior == "crash":
    note("end")
    sys.exit(3)
elif behavior == "hang":
    time.sleep(60)
note("end")
'''


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


class HeadlessFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.target = base / "target"
        self.target.mkdir()
        git("-C", str(self.target), "init", "-q")
        git("-C", str(self.target), "remote", "add", "origin", "git@github.com:acme/target.git")
        (self.target / "README.md").write_text("# target\n")
        git("-C", str(self.target), "add", "README.md")
        git("-C", str(self.target), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init")
        self.state = base / "state"
        self.bin = base / "bin"
        self.bin.mkdir()
        self.pi = self.bin / "pi"
        self.pi.write_text(FAKE_PI)
        self.pi.chmod(0o755)
        self._env = dict(os.environ)
        os.environ[provenance.STATE_ENV] = str(self.state)
        os.environ["FAKE_PI_TOOLS"] = str(REPO_ROOT / "tools")
        os.environ["PI_CODING_AGENT_DIR"] = str(base / "agent")
        os.environ["PATH"] = f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"
        os.environ.pop("FAKE_PI_BEHAVIOR", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def start(self, execution_id="exec-1", behavior="finish", **kwargs):
        os.environ["FAKE_PI_BEHAVIOR"] = behavior
        return hr.start(target=self.target, explicit_repo="acme/target", execution_id=execution_id,
                        objective="Inspect the repository and report", pi_executable=str(self.pi), **kwargs)

    def status(self, execution_id="exec-1"):
        return hr.status(target=self.target, explicit_repo="acme/target", execution_id=execution_id)


class HeadlessRunTest(HeadlessFixture):
    def test_finished_run_is_recorded_and_target_untouched(self):
        before = session_scope.snapshot(str(self.target))["digest"]
        record = self.start()
        self.assertEqual(record["status"], "exited")
        self.assertEqual(record["exit_code"], 0)
        self.assertEqual(record["run"]["status"], "PARTIAL")
        self.assertTrue(record["run_id"].startswith("run-"))
        self.assertEqual(record["launches"], 1)
        self.assertEqual(record["target_repository"], "acme/target")
        self.assertEqual(session_scope.snapshot(str(self.target))["digest"], before)
        path = self.state / "executions"
        self.assertEqual(len(list(path.rglob("exec-1.json"))), 1)
        self.assertTrue(Path(record["log_path"]).is_file())
        self.assertTrue(Path(record["log_path"]).read_text().startswith('{"type": "fake"'))
        self.assertTrue(Path(record["run"]["trace_path"]).is_file())
        self.assertFalse((self.target / ".pi").exists())
        self.assertEqual(sorted(p.name for p in self.target.iterdir()), [".git", "README.md"])

    def test_repeated_start_is_idempotent(self):
        first = self.start()
        second = self.start()
        self.assertEqual(second["outcome"], "already_finished")
        self.assertEqual(second["launches"], 1)
        self.assertEqual(second["run_id"], first["run_id"])
        runs = list((self.state / "runs").glob("run-*.json"))
        self.assertEqual(len(runs), 1)

    def test_exit_without_outcome_is_blocked(self):
        record = self.start(behavior="settle")
        self.assertEqual(record["run"]["status"], "BLOCKED")
        self.assertEqual(record["run"]["blocker_code"], "ORCHESTRATION_OUTCOME_UNRECORDED")

    def test_crash_requires_explicit_resume_into_same_run(self):
        crashed = self.start(behavior="crash")
        self.assertEqual(crashed["status"], "interrupted")
        self.assertEqual(crashed["exit_code"], 3)
        self.assertEqual(crashed["run"]["status"], "IN_PROGRESS")
        # A crashed execution never relaunches implicitly.
        again = self.start(behavior="finish")
        self.assertEqual(again["outcome"], "resume_required")
        self.assertEqual(again["launches"], 1)
        # The crashed process still names the lease: stale, recoverable only by an explicit takeover.
        self.assertEqual(crashed["run"]["lease"]["state"], "stale")
        self.assertEqual(crashed["run"]["lease"]["stale_reason"], "OWNER_PROCESS_DEAD")
        with self.assertRaises(hr.HeadlessError) as refused:
            self.start(behavior="finish", resume=True)
        self.assertEqual(refused.exception.code, "RUN_LEASE_STALE")
        self.assertEqual(self.status()["launches"], 1)
        with self.assertRaises(hr.HeadlessError) as invalid:
            self.start(behavior="finish", takeover=True)
        self.assertEqual(invalid.exception.code, "TAKEOVER_REQUIRES_RESUME")
        resumed = self.start(behavior="finish", resume=True, takeover=True)
        self.assertEqual(resumed["outcome"], "resumed")
        trace = provenance.get_run(hr.ROOT, crashed["run_id"])["parent_orchestration"]
        self.assertEqual([item["reason"] for item in trace["lease_takeovers"]], ["HEADLESS_RESUME_TAKEOVER"])
        self.assertEqual(trace["lease_takeovers"][0]["stale_reason"], "OWNER_PROCESS_DEAD")
        self.assertEqual([check["status"] for check in trace["resume_checks"]], ["verified"])
        # The explicit outcome released the lease.
        self.assertIsNone(resumed["run"]["lease"])
        self.assertEqual(resumed["launches"], 2)
        self.assertEqual(resumed["run_id"], crashed["run_id"])
        self.assertEqual(resumed["run"]["status"], "PARTIAL")
        self.assertEqual(len(list((self.state / "runs").glob("run-*.json"))), 1)
        # A finished run cannot be resumed again.
        final = self.start(behavior="finish", resume=True)
        self.assertEqual(final["outcome"], "already_finished")
        self.assertEqual(final["launches"], 2)

    def test_takeover_never_displaces_a_running_holder(self):
        crashed = self.start(behavior="crash")
        path = run_lease.lease_path(crashed["run_id"], hr.ROOT)
        lease = json.loads(path.read_text())
        # Pretend the holder is a live process on this machine whose heartbeat expired.
        lease.update({"pid": os.getpid(), "pid_start": run_lease.pid_start(os.getpid()),
                      "heartbeat_at": "2026-01-01T00:00:00Z", "expires_at": "2026-01-01T00:05:00Z"})
        path.write_text(json.dumps(lease))
        with self.assertRaises(hr.HeadlessError) as alive:
            self.start(behavior="finish", resume=True, takeover=True)
        self.assertEqual(alive.exception.code, "RUN_LEASE_HOLDER_ALIVE")
        self.assertEqual(self.status()["launches"], 1)
        self.assertNotIn("lease_takeovers", provenance.get_run(hr.ROOT, crashed["run_id"])["parent_orchestration"])

    def test_budget_refusal_before_spawn_never_takes_or_leaks_a_lease(self):
        crashed = self.start(behavior="crash")
        before = run_lease.status(crashed["run_id"], repo_root=hr.ROOT)

        def refuse():
            raise hr.HeadlessError("WORKSPACE_BUDGET_EXHAUSTED", "budget spent")
        with self.assertRaises(hr.HeadlessError) as refused:
            self.start(behavior="finish", resume=True, takeover=True, before_spawn=refuse)
        self.assertEqual(refused.exception.code, "WORKSPACE_BUDGET_EXHAUSTED")
        after = run_lease.status(crashed["run_id"], repo_root=hr.ROOT)
        self.assertEqual((after["state"], after["lease"]), (before["state"], before["lease"]))
        self.assertNotIn("lease_takeovers", provenance.get_run(hr.ROOT, crashed["run_id"])["parent_orchestration"])
        self.assertEqual(self.status()["launches"], 1)

    def test_execution_liveness_rejects_a_reused_pid(self):
        launched = self.start(behavior="hang", wait=False)
        try:
            self.assertEqual(launched["pid_start"], run_lease.pid_start(launched["pid"]))
            self.assertTrue(hr.execution_alive(launched))
            if launched["pid_start"] is not None:
                # Same pid, different start time: another process now owns the pid.
                self.assertFalse(hr.execution_alive({**launched, "pid_start": "1"}))
            # Records written before the start time was kept still use the plain pid probe.
            self.assertTrue(hr.execution_alive({**launched, "pid_start": None}))
        finally:
            hr.cancel(target=self.target, explicit_repo="acme/target", execution_id="exec-1", grace=1)
        self.assertFalse(hr.execution_alive(launched))

    def test_cancel_never_signals_a_reused_pid(self):
        launched = self.start(behavior="hang", wait=False)
        if launched["pid_start"] is None:
            self.skipTest("no /proc: pid start time unavailable")
        path = hr.execution_path(session_scope.common_dir(self.target), "exec-1")
        try:
            # The pid is alive but its start time no longer matches: another process reused it.
            record = json.loads(path.read_text())
            path.write_text(json.dumps({**record, "pid_start": "1"}))
            with unittest.mock.patch.object(hr.os, "killpg") as killpg:
                cancelled = hr.cancel(target=self.target, explicit_repo="acme/target", execution_id="exec-1", grace=0.2)
            killpg.assert_not_called()
            self.assertEqual((cancelled["status"], cancelled["outcome"]), ("cancelled", "cancelled"))
            self.assertEqual(cancelled["child_gone_reason"], "PID_REUSED")
            self.assertIsNone(cancelled["pid"])
            self.assertTrue(hr.pid_alive(launched["pid"]), "the process holding the pid was left alone")
        finally:
            try:
                os.killpg(launched["pid"], 9)
            except ProcessLookupError:
                pass

    def test_failed_lease_handoff_stops_the_child_visibly(self):
        crashed = self.start(behavior="crash")
        real = run_lease.acquire

        def refuse_handoff(*args, **kwargs):
            if kwargs.get("handoff"):
                raise run_lease.LeaseError("RUN_LEASE_LOCK_TIMEOUT", "busy")
            return real(*args, **kwargs)
        with unittest.mock.patch.object(hr.run_lease, "acquire", side_effect=refuse_handoff):
            with self.assertRaises(hr.HeadlessError) as failed:
                self.start(behavior="hang", resume=True, takeover=True, wait=False)
        self.assertEqual(failed.exception.code, "RUN_LEASE_HANDOFF_FAILED")
        record = self.status()
        self.assertEqual((record["status"], record["lease_handoff_error"]), ("interrupted", "RUN_LEASE_LOCK_TIMEOUT"))
        self.assertIsNone(record["pid"])
        self.assertEqual(record["run_id"], crashed["run_id"])
        self.assertEqual(hr._OWN_CHILDREN, {}, "a stopped child is not left in the registry")

    def test_interrupted_process_is_detected_and_resumable(self):
        launched = self.start(behavior="hang", wait=False)
        self.assertEqual(launched["status"], "launched")
        self.assertTrue(hr.pid_alive(launched["pid"]))
        running = self.start(behavior="hang", wait=False)
        self.assertEqual(running["outcome"], "already_running")
        self.assertEqual(running["launches"], 1)
        deadline = time.monotonic() + 5
        while hr.discover_run_id(Path(launched["session_dir"]), launched["session_id"]) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        os.killpg(launched["pid"], 9)
        deadline = time.monotonic() + 5
        while hr.pid_alive(launched["pid"]) and time.monotonic() < deadline:
            time.sleep(0.05)
        # Reap so the pid is not a zombie that still answers signal 0.
        try:
            os.waitpid(launched["pid"], 0)
        except ChildProcessError:
            pass
        observed = self.status()
        self.assertEqual(observed["status"], "interrupted")
        blocked = self.start(behavior="finish")
        self.assertEqual(blocked["outcome"], "resume_required")
        resumed = self.start(behavior="finish", resume=True, takeover=True)
        self.assertEqual(resumed["run"]["status"], "PARTIAL")
        self.assertEqual(resumed["run_id"], observed["run_id"])

    def test_cancel_stops_the_process_and_closes_the_run(self):
        launched = self.start(behavior="hang", wait=False)
        deadline = time.monotonic() + 5
        while hr.discover_run_id(Path(launched["session_dir"]), launched["session_id"]) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        cancelled = hr.cancel(target=self.target, explicit_repo="acme/target", execution_id="exec-1", grace=2)
        try:
            os.waitpid(launched["pid"], 0)
        except ChildProcessError:
            pass
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse(hr.pid_alive(launched["pid"]))
        self.assertEqual(cancelled["run"]["status"], "BLOCKED")
        self.assertEqual(cancelled["run"]["blocker_code"], "EXECUTION_CANCELLED")
        again = self.start(behavior="finish")
        self.assertEqual(again["outcome"], "already_finished")

    def test_concurrent_starts_with_one_id_launch_at_most_one_child(self):
        # Widen the window between reading the record and launching, where an
        # unlocked start would let both callers see "not running".
        original = hr.mutation_permitted

        def slow_check(worktree):
            time.sleep(0.3)
            return original(worktree)

        barrier = threading.Barrier(2)
        results, errors = [], []

        def launch():
            barrier.wait()
            try:
                results.append(self.start(behavior="hang", wait=False))
            except Exception as exc:  # surfaced below
                errors.append(exc)

        os.environ["FAKE_PI_BEHAVIOR"] = "hang"
        with unittest.mock.patch.object(hr, "mutation_permitted", slow_check):
            threads = [threading.Thread(target=launch) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        pids = {r["pid"] for r in results if r.get("outcome") == "launched"}
        try:
            self.assertEqual(errors, [])
            self.assertEqual(sorted(r["outcome"] for r in results), ["already_running", "launched"])
            self.assertEqual(len(pids), 1)
            self.assertEqual(self.status()["launches"], 1)
        finally:
            for pid in pids:
                try:
                    os.killpg(pid, 9)
                    os.waitpid(pid, 0)
                except (ProcessLookupError, ChildProcessError):
                    pass

    def test_mutating_policy_requires_explicit_authorization(self):
        (self.target / ".pi").mkdir()
        shutil.copy(REPO_ROOT / "templates" / "autonomy.yaml", self.target / ".pi" / "autonomy.yaml")
        with self.assertRaises(hr.HeadlessError) as ctx:
            self.start()
        self.assertEqual(ctx.exception.code, "MUTATION_NOT_AUTHORIZED")
        self.assertIn("commit", ctx.exception.detail)
        self.assertEqual(len(list((self.state / "runs").glob("run-*.json"))) if (self.state / "runs").exists() else 0, 0)
        record = self.start(allow_mutation=True)
        self.assertEqual(record["run"]["status"], "PARTIAL")
        self.assertTrue(record["mutation_allowed"])

    def test_repository_identity_fails_closed(self):
        with self.assertRaises(hr.HeadlessError) as ctx:
            hr.start(target=self.target, explicit_repo="acme/other", execution_id="exec-1", objective="x",
                     pi_executable=str(self.pi))
        self.assertEqual(ctx.exception.code, "TARGET_REPOSITORY_CONFLICT")
        with self.assertRaises(hr.HeadlessError) as ctx:
            hr.start(target=self.target, explicit_repo="acme/target", execution_id="../escape", objective="x",
                     pi_executable=str(self.pi))
        self.assertEqual(ctx.exception.code, "EXECUTION_ID_INVALID")
        with self.assertRaises(hr.HeadlessError) as ctx:
            hr.start(target=self.target, explicit_repo="acme/target", execution_id="exec-1",
                     objective="use ghp_abcdefghijklmnopqrstuvwxyz1234 please", pi_executable=str(self.pi))
        self.assertEqual(ctx.exception.code, "OBJECTIVE_INVALID")
        with self.assertRaises(hr.HeadlessError) as ctx:
            self.status("never-started")
        self.assertEqual(ctx.exception.code, "EXECUTION_UNKNOWN")
        with self.assertRaises(hr.HeadlessError) as ctx:
            self.start(resume=True)
        self.assertEqual(ctx.exception.code, "EXECUTION_UNKNOWN")

    def test_execution_id_is_bound_to_one_repository(self):
        self.start()
        other = Path(self._tmp.name) / "other"
        other.mkdir()
        git("-C", str(other), "init", "-q")
        git("-C", str(other), "remote", "add", "origin", "git@github.com:acme/other.git")
        # A different repository has a different state key, so the same id is simply unknown there.
        with self.assertRaises(hr.HeadlessError) as ctx:
            hr.status(target=other, explicit_repo="acme/other", execution_id="exec-1")
        self.assertEqual(ctx.exception.code, "EXECUTION_UNKNOWN")

    def test_checkout_is_restored_and_clean_worktrees_removed(self):
        os.environ["FAKE_PI_MESS"] = "clean"
        record = self.start()
        cleanup = record["checkout"]
        self.assertTrue(cleanup["restored"])
        self.assertEqual(cleanup["branch_after"], cleanup["branch_before"])
        self.assertFalse(cleanup["branch_before"].startswith("pico/"))
        self.assertEqual(git("-C", str(self.target), "branch", "--show-current"), cleanup["branch_before"])
        self.assertEqual(len(cleanup["removed_worktrees"]), 1)
        self.assertEqual(len(git("-C", str(self.target), "worktree", "list").splitlines()), 1)
        branches = git("-C", str(self.target), "branch", "--format=%(refname:short)").split()
        self.assertIn("pico/batch-1", branches)  # the work is kept on its branches
        self.assertIn("pico/task-1", branches)

    def test_dirty_worktrees_and_checkouts_are_never_touched(self):
        os.environ["FAKE_PI_MESS"] = "dirty"
        record = self.start()
        self.assertEqual((len(record["checkout"]["kept_worktrees"]), record["checkout"]["removed_worktrees"]), (1, []))
        self.assertTrue(record["checkout"]["restored"])  # the parent checkout itself was clean
        os.environ["FAKE_PI_MESS"] = "clean"
        git("-C", str(self.target), "branch", "-D", "pico/batch-1")
        (self.target / "README.md").write_text("edited but not committed\n")
        os.environ["FAKE_PI_BEHAVIOR"] = "finish"
        record = hr.start(target=self.target, explicit_repo="acme/target", execution_id="exec-dirty",
                          objective="Inspect", pi_executable=str(self.pi))
        self.assertFalse(record["checkout"]["restored"])
        self.assertEqual(record["checkout"]["left_reason"], "uncommitted changes in the checkout")

    def test_changes_left_in_the_parent_checkout_are_recorded_and_kept(self):
        os.environ["FAKE_PI_MESS"] = "clean"
        clean = self.start()
        self.assertEqual((clean["checkout"]["dirty_files"], clean["checkout"]["dirty_count"]), ([], 0))
        self.assertEqual((clean["checkout"]["new_untracked"], clean["checkout"]["new_untracked_count"]), ([], 0))
        os.environ["FAKE_PI_MESS"] = "parent-apply"
        os.environ["FAKE_PI_BEHAVIOR"] = "finish"
        record = hr.start(target=self.target, explicit_repo="acme/target", execution_id="exec-apply",
                          objective="Inspect", pi_executable=str(self.pi))
        self.assertEqual((record["checkout"]["dirty_files"], record["checkout"]["dirty_count"]), (["README.md"], 1))
        self.assertIn("applied in the parent checkout", (self.target / "README.md").read_text())  # never cleaned

    def test_new_untracked_entries_are_reported_but_not_refused_or_removed(self):
        (self.target / "scratch.txt").write_text("there before the run\n")
        os.environ["FAKE_PI_MESS"] = "parent-new-file"
        record = self.start()
        cleanup = record["checkout"]
        # Only what appeared during the run, an untracked directory as one entry, PICO's .pi/worktrees/ excluded.
        self.assertEqual((cleanup["new_untracked"], cleanup["new_untracked_count"]), (["generated/"], 1))
        self.assertEqual((cleanup["dirty_files"], cleanup["dirty_count"]), ([], 0))
        self.assertTrue((self.target / "generated" / "slug.py").is_file())

    def test_tracked_changes_ignore_untracked_files_and_name_renames_once(self):
        self.assertEqual(hr.tracked_changes(self.target), [])
        (self.target / "scratch.txt").write_text("untracked\n")
        self.assertEqual(hr.tracked_changes(self.target), [])
        git("-C", str(self.target), "mv", "README.md", "README.rst")
        self.assertEqual(hr.tracked_changes(self.target), ["README.rst"])
        self.assertIsNone(hr.tracked_changes(Path(self._tmp.name) / "not-a-checkout"))

    def test_resume_prompt_never_carries_the_run_marker(self):
        self.assertNotIn("PI_ORCHESTRATION_RUN_V1", hr.RESUME_PROMPT)
        self.assertIn("orchestration_record", hr.RESUME_PROMPT)
        # The model must be able to match the lifecycle's visible status text.
        self.assertIn("is ready", hr.RESUME_PROMPT)
        self.assertIn("explicit outcome", hr.RESUME_PROMPT)

    def test_cancel_closes_run_before_a_graceful_shutdown_can_settle_it(self):
        launched = self.start(behavior="hang", wait=False)
        deadline = time.monotonic() + 5
        while hr.discover_run_id(Path(launched["session_dir"]), launched["session_id"]) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        run_id = hr.discover_run_id(Path(launched["session_dir"]), launched["session_id"])
        signals = []
        real_killpg = os.killpg
        def observe(pid, sig):
            # At signal time the run must already be terminal with the cancellation code.
            signals.append(hr.run_status(run_id)["blocker_code"])
            real_killpg(pid, sig)
        with unittest.mock.patch.object(hr.os, "killpg", side_effect=observe):
            hr.cancel(target=self.target, explicit_repo="acme/target", execution_id="exec-1", grace=2)
        try:
            os.waitpid(launched["pid"], 0)
        except ChildProcessError:
            pass
        self.assertEqual(signals[0], "EXECUTION_CANCELLED")

    def test_prompt_carries_marker_and_objective(self):
        prompt = hr.build_prompt("Review the parser")
        self.assertIn("PI_ORCHESTRATION_RUN_V1", prompt)
        self.assertIn("Review the parser", prompt)
        self.assertNotIn("$ARGUMENTS", prompt)
        self.assertFalse(prompt.startswith("---"))

    def test_cli(self):
        def run(args):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = hr.main(args)
            return rc, json.loads(out.getvalue())
        os.environ["FAKE_PI_BEHAVIOR"] = "settle"
        rc, data = run(["start", "--target", str(self.target), "--repo", "acme/target", "--execution-id", "cli-1",
                        "--objective", "inspect", "--pi", str(self.pi)])
        self.assertEqual(rc, 1)
        self.assertEqual(data["run"]["blocker_code"], "ORCHESTRATION_OUTCOME_UNRECORDED")
        os.environ["FAKE_PI_BEHAVIOR"] = "finish"
        rc, data = run(["start", "--target", str(self.target), "--repo", "acme/target", "--execution-id", "cli-2",
                        "--objective", "inspect", "--pi", str(self.pi)])
        self.assertEqual(rc, 0)
        self.assertTrue(data["ok"])
        rc, data = run(["status", "--target", str(self.target), "--repo", "acme/target", "--execution-id", "cli-2"])
        self.assertEqual(rc, 0)
        rc, data = run(["status", "--target", str(self.target), "--repo", "acme/target", "--execution-id", "nope"])
        self.assertEqual(rc, 1)
        self.assertEqual(data["error_code"], "EXECUTION_UNKNOWN")


REAL_BOOT_ELAPSED = hr.boot_elapsed


class FakeClock:
    """Injectable wall clock: tests move time explicitly instead of sleeping."""

    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class ExecutionTimeLimitTest(HeadlessFixture):
    """Wall-clock limit per execution, with a fake child (`hang`) and a fake clock."""

    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        patch = unittest.mock.patch.object(hr, "clock", self.clock)
        patch.start()
        self.addCleanup(patch.stop)
        # The fake wall clock drives these tests; test_boot_clock_ignores_wall_clock_jumps covers /proc.
        boot = unittest.mock.patch.object(hr, "boot_elapsed", lambda record: None)
        boot.start()
        self.addCleanup(boot.stop)

    def launch_hanging(self, execution_id="exec-1", **kwargs):
        launched = self.start(execution_id=execution_id, behavior="hang", wait=False, **kwargs)
        deadline = time.monotonic() + 5
        while hr.discover_run_id(Path(launched["session_dir"]), launched["session_id"]) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.addCleanup(self.reap, launched["pid"])
        return launched

    @staticmethod
    def reap(pid):
        try:
            os.killpg(pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass

    def test_no_limit_means_no_deadline_and_no_stop(self):
        launched = self.launch_hanging()
        for key in ("max_duration_seconds", "deadline_at", "deadline_epoch"):
            self.assertNotIn(key, launched)
        self.clock.now += 10 ** 7
        record = self.status()
        self.assertEqual((record["status"], record["outcome"]), ("launched", "launched"))
        self.assertNotIn("stop_reason", record)
        self.assertTrue(hr.execution_alive(record))

    def test_limit_is_recorded_at_launch(self):
        launched = self.launch_hanging(max_duration_seconds=90)
        self.assertEqual(launched["max_duration_seconds"], 90)
        self.assertEqual(launched["deadline_epoch"], self.clock.now + 90)
        self.assertRegex(launched["deadline_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(hr.time_limit_remaining(launched), 90)

    def test_under_the_limit_the_child_keeps_running(self):
        launched = self.launch_hanging(max_duration_seconds=100)
        self.clock.now += 99.9
        record = self.status()
        self.assertEqual(record["status"], "launched")
        self.assertTrue(hr.pid_alive(launched["pid"]))
        self.assertEqual(record["run"]["status"], "IN_PROGRESS")

    def test_at_the_limit_the_child_is_cancelled_with_a_distinct_reason(self):
        launched = self.launch_hanging(max_duration_seconds=100)
        self.clock.now += 100
        record = self.status()
        self.assertEqual((record["status"], record["outcome"]), ("cancelled", "time_limit"))
        self.assertEqual(record["stop_reason"], "EXECUTION_TIME_LIMIT")
        self.assertEqual(record["time_limit"]["max_duration_seconds"], 100)
        self.assertEqual(record["time_limit"]["elapsed_seconds"], 100.0)
        self.assertIsNone(record["pid"])
        self.assertFalse(hr.execution_alive(launched))
        # The parent run is not left in progress: BLOCKED with the limit's code, not the operator cancel's.
        self.assertEqual(record["run"]["status"], "BLOCKED")
        self.assertEqual(record["run"]["blocker_code"], "EXECUTION_TIME_LIMIT")
        # Stopped is final for this execution id until an explicit new launch.
        again = self.status()
        self.assertEqual((again["status"], again["stop_reason"]), ("cancelled", "EXECUTION_TIME_LIMIT"))

    def test_over_the_limit_is_enforced_the_same_way(self):
        self.launch_hanging(max_duration_seconds=5)
        self.clock.now += 3600
        record = self.status()
        self.assertEqual(record["stop_reason"], "EXECUTION_TIME_LIMIT")
        self.assertEqual(record["time_limit"]["elapsed_seconds"], 3600.0)

    def test_a_reused_pid_is_never_signalled_at_the_deadline(self):
        launched = self.launch_hanging(max_duration_seconds=10)
        if launched["pid_start"] is None:
            self.skipTest("no /proc: pid start time unavailable")
        path = hr.execution_path(session_scope.common_dir(self.target), "exec-1")
        record = json.loads(path.read_text())
        path.write_text(json.dumps({**record, "pid_start": "1"}))
        self.clock.now += 11
        with unittest.mock.patch.object(hr.os, "killpg") as killpg:
            observed = self.status()
        killpg.assert_not_called()
        self.assertNotEqual(observed["status"], "cancelled")
        self.assertEqual(observed["child_gone_reason"], "PID_REUSED")
        self.assertNotIn("stop_reason", observed)
        self.assertTrue(hr.pid_alive(launched["pid"]), "the process holding the pid was left alone")

    def test_a_child_that_ended_before_the_deadline_keeps_its_own_outcome(self):
        finished = self.start(behavior="finish", max_duration_seconds=100)
        self.clock.now += 1000
        record = self.status()
        self.assertEqual((record["status"], record["run"]["status"]), ("exited", "PARTIAL"))
        self.assertNotIn("stop_reason", record)
        self.assertEqual(record["max_duration_seconds"], finished["max_duration_seconds"])

    def test_a_waiting_launcher_enforces_the_limit_itself(self):
        # Pass the deadline only once the child has created its parent run: on a
        # slow runner a fixed delay can fire before that, leaving no run to settle.
        stop = threading.Event()

        def expire():
            deadline = time.monotonic() + 20
            while not stop.is_set() and time.monotonic() < deadline:
                if list((self.state / "runs").glob("run-*.json")):
                    self.clock.now += 5
                    return
                time.sleep(0.05)

        timer = threading.Thread(target=expire, daemon=True)
        timer.start()
        self.addCleanup(stop.set)
        started = time.monotonic()
        record = self.start(behavior="hang", max_duration_seconds=1)
        self.assertLess(time.monotonic() - started, 5, "the launcher returned because of the limit, not the child's own end")
        self.assertEqual((record["status"], record["outcome"]), ("cancelled", "time_limit"))
        self.assertEqual(record["stop_reason"], "EXECUTION_TIME_LIMIT")
        self.assertEqual(record["run"]["status"], "BLOCKED")
        self.assertEqual(record["run"]["blocker_code"], "EXECUTION_TIME_LIMIT")
        self.assertIsNone(record["pid"])
        self.assertLess(record["exit_code"], 0, "a killed child reports its signal, never 0")

    def test_a_waiting_launcher_records_the_signal_when_cancelled_from_elsewhere(self):
        results = []
        thread = threading.Thread(target=lambda: results.append(self.start(behavior="hang")))
        thread.start()
        path = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            found = list((self.state / "executions").rglob("exec-1.json"))
            if found and json.loads(found[0].read_text()).get("status") == "launched":
                path = found[0]
                break
            time.sleep(0.05)
        self.assertIsNotNone(path)
        while hr.discover_run_id(Path(json.loads(path.read_text())["session_dir"]),
                                 json.loads(path.read_text())["session_id"]) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        hr.cancel(target=self.target, explicit_repo="acme/target", execution_id="exec-1", grace=2)
        thread.join(10)
        self.assertEqual((results[0]["status"], results[0]["outcome"]), ("cancelled", "cancelled"))
        self.assertLess(results[0]["exit_code"], 0)

    def test_boot_clock_ignores_wall_clock_jumps(self):
        launched = self.launch_hanging(max_duration_seconds=100)
        if launched["pid_start"] is None or not Path("/proc/uptime").exists():
            self.skipTest("no /proc")
        with unittest.mock.patch.object(hr, "boot_elapsed", REAL_BOOT_ELAPSED):
            self.clock.now += 10 ** 6  # the wall clock jumps a million seconds: the child has not run that long
            self.assertEqual(self.status()["status"], "launched")
            self.assertLess(hr.boot_elapsed(launched), 30)
            self.assertEqual(round(hr.time_limit_remaining(launched)), round(100 - hr.boot_elapsed(launched)))
            # And a child that really ran past its limit on the boot clock is stopped with the wall clock untouched.
            with unittest.mock.patch.object(hr, "boot_elapsed", lambda record: 101.0):
                self.assertEqual(self.status()["stop_reason"], "EXECUTION_TIME_LIMIT")

    def test_invalid_limits_are_refused_before_anything_launches(self):
        for bad in (0, -5, True, "60", 1.5):
            with self.assertRaises(hr.HeadlessError) as refused:
                self.start(behavior="hang", wait=False, max_duration_seconds=bad)
            self.assertEqual(refused.exception.code, "MAX_DURATION_INVALID")
        self.assertEqual(list((self.state / "executions").rglob("*.json")), [])

    def test_resume_after_a_crash_gets_a_fresh_window_from_its_own_argument(self):
        crashed = self.start(behavior="crash", max_duration_seconds=50)
        self.assertEqual(crashed["status"], "interrupted")
        self.clock.now += 500  # long past the first launch's deadline
        resumed = self.start(behavior="hang", wait=False, resume=True, takeover=True, max_duration_seconds=70)
        self.addCleanup(self.reap, resumed["pid"])
        self.assertEqual(resumed["deadline_epoch"], self.clock.now + 70)
        self.assertEqual(resumed["run_id"], crashed["run_id"])
        self.assertEqual(self.status()["status"], "launched")
        self.clock.now += 70
        stopped = self.status()
        self.assertEqual(stopped["stop_reason"], "EXECUTION_TIME_LIMIT")
        # A resume without --max-duration does not inherit the earlier limit.
        other = self.start(execution_id="exec-2", behavior="crash", max_duration_seconds=50)
        self.assertEqual(other["status"], "interrupted")
        again = self.start(execution_id="exec-2", behavior="hang", wait=False, resume=True, takeover=True)
        self.addCleanup(self.reap, again["pid"])
        self.assertNotIn("deadline_epoch", again)
        self.assertNotIn("max_duration_seconds", again)

    def test_resume_after_a_time_limit_stop_needs_a_new_execution(self):
        self.launch_hanging(max_duration_seconds=10)
        self.clock.now += 10
        stopped = self.status()
        self.assertEqual(stopped["stop_reason"], "EXECUTION_TIME_LIMIT")
        # The parent run was settled BLOCKED, so neither a repeat start nor a resume resurrects it.
        for resume in (False, True):
            result = self.start(behavior="finish", resume=resume, takeover=resume, max_duration_seconds=10)
            self.assertEqual(result["outcome"], "already_finished")
            self.assertEqual(result["launches"], 1)
            self.assertEqual(result["stop_reason"], "EXECUTION_TIME_LIMIT")
        # An explicit new launch (a new execution id, hence a new run) starts its own window.
        fresh = self.start(execution_id="exec-new", behavior="hang", wait=False, max_duration_seconds=10)
        self.addCleanup(self.reap, fresh["pid"])
        self.assertEqual(fresh["deadline_epoch"], self.clock.now + 10)

    def test_cancel_after_a_time_limit_stop_keeps_the_outcome(self):
        self.launch_hanging(max_duration_seconds=10)
        self.clock.now += 10
        self.status()
        cancelled = hr.cancel(target=self.target, explicit_repo="acme/target", execution_id="exec-1", grace=1)
        self.assertEqual((cancelled["status"], cancelled["outcome"]), ("cancelled", "time_limit"))
        self.assertEqual(cancelled["run"]["blocker_code"], "EXECUTION_TIME_LIMIT")

    def test_cli_max_duration(self):
        def run(args):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = hr.main(args)
            return rc, json.loads(out.getvalue())
        base = ["--target", str(self.target), "--repo", "acme/target"]
        os.environ["FAKE_PI_BEHAVIOR"] = "hang"
        rc, data = run(["start", *base, "--pi", str(self.pi), "--execution-id", "cli-1", "--objective", "inspect",
                        "--no-wait", "--max-duration", "120"])
        self.assertEqual(rc, 0)
        self.addCleanup(self.reap, data["pid"])
        self.assertEqual(data["max_duration_seconds"], 120)
        self.assertEqual(data["deadline_epoch"], self.clock.now + 120)
        rc, data = run(["start", *base, "--pi", str(self.pi), "--execution-id", "cli-2", "--objective", "inspect",
                        "--no-wait", "--max-duration", "0"])
        self.assertEqual((rc, data["error_code"]), (1, "MAX_DURATION_INVALID"))
        self.clock.now += 120
        rc, data = run(["status", *base, "--execution-id", "cli-1"])
        self.assertEqual(data["stop_reason"], "EXECUTION_TIME_LIMIT")
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
