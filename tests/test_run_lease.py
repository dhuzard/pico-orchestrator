"""Single-owner run lease (audit A3) and verified resume checkpoint (audit A10).

Deterministic: real Git repositories in temporary directories, real
subprocesses for contention, no Pi, no network, no GitHub.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import parent_orchestration as po  # noqa: E402
import provenance  # noqa: E402
import run_lease  # noqa: E402
import session_scope  # noqa: E402

LEASE_CLI = ROOT / "tools" / "run_lease.py"
PARENT_CLI = ROOT / "tools" / "parent_orchestration.py"
REPO = "Examplecorp/examplelab"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


class StateTestCase(unittest.TestCase):
    """A records root with the run schema, a Git target, and no ambient state directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.records = base / "orchestrator"
        (self.records / "schemas").mkdir(parents=True)
        shutil.copy(ROOT / "schemas/run-record.schema.json", self.records / "schemas/run-record.schema.json")
        self.target = base / "target"
        self.target.mkdir()
        git(self.target, "init", "-q", "-b", "main")
        git(self.target, "remote", "add", "origin", f"git@github.com:{REPO}.git")
        (self.target / "README.md").write_text("# target\n")
        git(self.target, "add", "README.md")
        self.commit("init")
        env = unittest.mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(provenance.STATE_ENV, None)
        os.environ.pop("PI_TARGET_REPOSITORY", None)

    def tearDown(self):
        self._tmp.cleanup()

    def commit(self, message: str) -> str:
        git(self.target, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q",
            "--allow-empty", "-m", message)
        return git(self.target, "rev-parse", "HEAD")

    def start_run(self, owner: str | None = None) -> str:
        started = po.start(worktree=self.target, explicit_repo=REPO, repo_root=self.records,
                           owner_id=owner, pid=os.getpid() if owner else None)
        self.assertEqual(started["status"], "IN_PROGRESS", started)
        return started["run_id"]

    def parent(self, run_id: str) -> dict:
        return provenance.get_run(self.records, run_id)["parent_orchestration"]

    def rewrite_lease(self, run_id: str, **fields) -> dict:
        """Simulate what time or another machine did to a lease file."""
        path = run_lease.lease_path(run_id, self.records)
        lease = json.loads(path.read_text())
        lease.update(fields)
        path.write_text(json.dumps(lease))
        return lease

    def expire(self, run_id: str) -> dict:
        return self.rewrite_lease(run_id, heartbeat_at="2026-01-01T00:00:00Z", expires_at="2026-01-01T00:05:00Z")


class RunLeaseTest(StateTestCase):
    RUN = "run-lease-test"

    def acquire(self, owner: str, **kwargs):
        return run_lease.acquire(self.RUN, owner, repo_root=self.records, **kwargs)

    def test_lease_records_owner_process_machine_heartbeat_and_expiry_in_state_not_target(self):
        result = self.acquire("owner-a", pid=os.getpid(), ttl_seconds=60, now=1_000_000.0)
        lease = result["lease"]
        self.assertEqual(result["acquired"], "new")
        self.assertEqual((lease["owner_id"], lease["pid"], lease["host"]), ("owner-a", os.getpid(), run_lease.host_name()))
        self.assertEqual(lease["host_id"], run_lease.host_id())
        self.assertEqual(lease["pid_start"], run_lease.pid_start(os.getpid()))
        if Path("/proc/self/stat").exists():
            self.assertIsNotNone(lease["pid_start"])
            self.assertIsNotNone(lease["host_id"])
        self.assertEqual(lease["acquired_at"], lease["heartbeat_at"])
        self.assertEqual(run_lease._epoch(lease["expires_at"]) - run_lease._epoch(lease["heartbeat_at"]), 60)
        path = run_lease.lease_path(self.RUN, self.records)
        self.assertEqual(path, self.records / "provenance" / "leases" / f"{self.RUN}.json")
        self.assertEqual(json.loads(path.read_text()), lease)
        self.assertFalse(any(self.target.rglob("*.lock")))
        with unittest.mock.patch.dict(os.environ, {provenance.STATE_ENV: str(Path(self._tmp.name) / "state")}):
            self.assertEqual(run_lease.lease_dir(self.records), Path(self._tmp.name) / "state" / "leases")

    def test_live_lease_blocks_a_second_owner_immediately(self):
        self.acquire("owner-a", pid=os.getpid())
        started = time.monotonic()
        with self.assertRaises(run_lease.LeaseError) as held:
            self.acquire("owner-b", pid=os.getpid())
        self.assertLess(time.monotonic() - started, 1.0, "acquire must not wait for the holder")
        self.assertEqual(held.exception.code, "RUN_LEASE_HELD")
        self.assertEqual(held.exception.lease["owner_id"], "owner-a")
        with self.assertRaises(run_lease.LeaseError) as refused:
            run_lease.takeover(self.RUN, "owner-b", reason="OPERATOR_RECOVERY", repo_root=self.records)
        self.assertEqual(refused.exception.code, "RUN_LEASE_HELD")

    def test_same_owner_renews_only_from_the_same_process_or_an_explicit_handoff(self):
        first = self.acquire("owner-a", pid=os.getpid(), now=1_000_000.0)["lease"]
        renewed = self.acquire("owner-a", pid=os.getpid(), now=1_000_030.0)
        self.assertEqual(renewed["acquired"], "renewed")
        self.assertEqual(renewed["lease"]["acquired_at"], first["acquired_at"])
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(child.kill)
        # An owner id collision from another process is refused.
        with self.assertRaises(run_lease.LeaseError) as mismatch:
            self.acquire("owner-a", pid=child.pid)
        self.assertEqual(mismatch.exception.code, "RUN_LEASE_PID_MISMATCH")
        # Even claiming a hand-off, a process that is neither the holder nor its child cannot take it.
        with self.assertRaises(run_lease.LeaseError) as unrelated:
            self.acquire("owner-a", pid=os.getppid(), handoff=True, caller_pid=dead_pid())
        self.assertEqual(unrelated.exception.code, "RUN_LEASE_PID_MISMATCH")
        # The launcher (the current holder) hands it to the process it started.
        handed = self.acquire("owner-a", pid=child.pid, handoff=True)
        self.assertEqual((handed["lease"]["pid"], handed["lease"]["pid_start"]),
                         (child.pid, run_lease.pid_start(child.pid)))
        with self.assertRaises(run_lease.LeaseError) as stale_pid:
            run_lease.heartbeat(self.RUN, "owner-a", pid=os.getpid(), repo_root=self.records)
        self.assertEqual(stale_pid.exception.code, "RUN_LEASE_PID_MISMATCH")

    def test_a_child_claims_its_launchers_lease_through_the_handoff(self):
        self.acquire("owner-a", pid=os.getpid())
        state = Path(self._tmp.name) / "state"
        env = {**os.environ, provenance.STATE_ENV: str(state)}
        run_lease.acquire(self.RUN, "owner-a", pid=os.getpid(), repo_root=self.records)
        shutil.copytree(run_lease.lease_dir(self.records), state / "leases")
        # The child process asks for itself, as the lifecycle in a launched Pi does.
        script = (f"import json, os, subprocess, sys; out = subprocess.run([sys.executable, {str(LEASE_CLI)!r}, "
                  f"'acquire', {self.RUN!r}, '--owner', 'owner-a', '--pid', str(os.getpid()), '--handoff'], "
                  "capture_output=True, text=True); print(out.stdout)")
        claimed = json.loads(subprocess.run([sys.executable, "-c", script], env=env, capture_output=True,
                                            text=True, check=True).stdout)
        self.assertTrue(claimed["ok"], claimed)
        self.assertEqual(claimed["acquired"], "renewed")
        script_without = script.replace(", '--handoff'", "")
        refused = json.loads(subprocess.run([sys.executable, "-c", script_without], env=env, capture_output=True,
                                            text=True, check=True).stdout)
        self.assertEqual(refused["error_code"], "RUN_LEASE_PID_MISMATCH")

    def test_concurrent_acquire_from_separate_processes_has_exactly_one_winner(self):
        state = Path(self._tmp.name) / "state"
        env = {**os.environ, provenance.STATE_ENV: str(state)}
        go = Path(self._tmp.name) / "go"
        script = ("import os, sys, time, runpy\n"
                  "while not os.path.exists(sys.argv[1]): time.sleep(0.001)\n"
                  "sys.argv = sys.argv[2:]\n"
                  "runpy.run_path(sys.argv[0], run_name='__main__')\n")
        children = [subprocess.Popen([sys.executable, "-c", script, str(go), str(LEASE_CLI), "acquire", self.RUN,
                                      "--owner", f"owner-{index}", "--pid", str(os.getpid())],
                                     env=env, stdout=subprocess.PIPE, text=True) for index in range(8)]
        time.sleep(0.3)
        go.write_text("go")
        results = [json.loads(child.communicate(timeout=60)[0]) for child in children]
        winners = [result for result in results if result["ok"]]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual({result["error_code"] for result in results if not result["ok"]}, {"RUN_LEASE_HELD"})
        stored = json.loads((state / "leases" / f"{self.RUN}.json").read_text())
        self.assertEqual(stored["owner_id"], winners[0]["lease"]["owner_id"])

    def test_simulated_contention_between_read_and_write_cannot_double_acquire(self):
        real_read = run_lease._read

        def slow_read(path):
            value = real_read(path)
            time.sleep(0.05)  # widen the check-then-write window
            return value
        with unittest.mock.patch.object(run_lease, "_read", side_effect=slow_read):
            with ThreadPoolExecutor(max_workers=6) as pool:
                outcomes = list(pool.map(lambda index: self._try(f"owner-{index}"), range(6)))
        self.assertEqual(outcomes.count("ok"), 1, outcomes)
        self.assertEqual(set(outcomes) - {"ok"}, {"RUN_LEASE_HELD"})

    def _try(self, owner: str) -> str:
        try:
            self.acquire(owner, pid=os.getpid())
            return "ok"
        except run_lease.LeaseError as exc:
            return exc.code

    def test_expired_heartbeat_is_stale_and_reported_not_recovered(self):
        self.acquire("owner-a", pid=dead_pid(), ttl_seconds=30, now=1_000_000.0)
        self.rewrite_lease(self.RUN, host="another-host")  # isolate the expiry from the pid probe
        self.assertEqual(run_lease.status(self.RUN, repo_root=self.records, now=1_000_029.0)["state"], "live")
        view = run_lease.status(self.RUN, repo_root=self.records, now=1_000_030.0)
        self.assertEqual((view["state"], view["stale_reason"]), ("stale", "HEARTBEAT_EXPIRED"))
        with self.assertRaises(run_lease.LeaseError) as stale:
            self.acquire("owner-b", pid=os.getpid(), now=1_000_031.0)
        self.assertEqual(stale.exception.code, "RUN_LEASE_STALE")

    def test_dead_or_reused_pid_on_this_machine_is_stale(self):
        self.acquire("owner-a", pid=dead_pid())
        view = run_lease.status(self.RUN, repo_root=self.records)
        self.assertEqual((view["state"], view["stale_reason"], view["holder_alive"]),
                         ("stale", "OWNER_PROCESS_DEAD", False))
        if run_lease.pid_start(os.getpid()) is not None:
            # The pid is alive, but it is not the process that took the lease (pid reuse).
            self.rewrite_lease(self.RUN, pid=os.getpid(), pid_start="1")
            self.assertEqual(run_lease.status(self.RUN, repo_root=self.records)["stale_reason"], "OWNER_PROCESS_DEAD")

    def test_pids_are_never_judged_across_machines_or_containers_sharing_a_hostname(self):
        self.acquire("owner-a", pid=dead_pid())
        self.rewrite_lease(self.RUN, host="another-host")
        self.assertEqual(run_lease.status(self.RUN, repo_root=self.records)["state"], "live")
        self.rewrite_lease(self.RUN, host=run_lease.host_name(), host_id="0000000000000000")
        view = run_lease.status(self.RUN, repo_root=self.records)
        self.assertEqual((view["state"], view["holder_alive"]), ("live", None))
        with unittest.mock.patch.object(run_lease, "_read_text", return_value=None), \
                unittest.mock.patch("os.readlink", side_effect=OSError):
            run_lease.host_id.cache_clear()
            try:
                self.assertIsNone(run_lease.host_id(), "without machine keys only the hostname identifies a host")
            finally:
                run_lease.host_id.cache_clear()

    def test_unreadable_or_implausible_lease_is_stale(self):
        path = run_lease.lease_path(self.RUN, self.records)
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        view = run_lease.status(self.RUN, repo_root=self.records)
        self.assertEqual((view["state"], view["stale_reason"]), ("stale", "LEASE_UNREADABLE"))
        with self.assertRaises(run_lease.LeaseError) as stale:
            self.acquire("owner-b")
        self.assertEqual(stale.exception.code, "RUN_LEASE_STALE")
        path.unlink()
        now = time.time()
        self.acquire("owner-a", pid=os.getpid())
        for fields in ({"expires_at": run_lease._iso(now + 3 * 24 * 3600)},  # beyond heartbeat + max TTL
                       {"heartbeat_at": run_lease._iso(now + 3600), "expires_at": run_lease._iso(now + 3900)},
                       {"expires_at": "not a time"}):
            with self.subTest(fields=fields):
                self.rewrite_lease(self.RUN, **{"heartbeat_at": run_lease._iso(now), **fields})
                self.assertEqual(run_lease.status(self.RUN, repo_root=self.records)["stale_reason"], "LEASE_UNREADABLE")

    def test_heartbeat_extends_only_the_owners_lease(self):
        self.acquire("owner-a", pid=os.getpid(), ttl_seconds=30, now=1_000_000.0)
        beat = run_lease.heartbeat(self.RUN, "owner-a", ttl_seconds=30, repo_root=self.records, now=1_000_020.0)
        self.assertEqual(beat["lease"]["pid"], os.getpid())
        self.assertEqual(run_lease.status(self.RUN, repo_root=self.records, now=1_000_045.0)["state"], "live")
        with self.assertRaises(run_lease.LeaseError) as lost:
            run_lease.heartbeat(self.RUN, "owner-b", repo_root=self.records)
        self.assertEqual(lost.exception.code, "RUN_LEASE_LOST")
        run_lease.release(self.RUN, "owner-a", repo_root=self.records)
        with self.assertRaises(run_lease.LeaseError) as missing:
            run_lease.heartbeat(self.RUN, "owner-a", repo_root=self.records)
        self.assertEqual(missing.exception.code, "RUN_LEASE_NOT_HELD")

    def test_release_is_owner_only(self):
        self.acquire("owner-a", pid=os.getpid())
        with self.assertRaises(run_lease.LeaseError) as lost:
            run_lease.release(self.RUN, "owner-b", repo_root=self.records)
        self.assertEqual(lost.exception.code, "RUN_LEASE_LOST")
        self.assertTrue(run_lease.release(self.RUN, "owner-a", repo_root=self.records)["released"])
        self.assertEqual(run_lease.status(self.RUN, repo_root=self.records)["state"], "free")
        self.assertFalse(run_lease.release(self.RUN, "owner-a", repo_root=self.records)["released"])
        self.assertFalse(hasattr(run_lease, "release_for_outcome"), "no owner-blind release exists")

    def test_invalid_inputs_fail_closed(self):
        for kwargs in ({"owner_id": "../escape"}, {"owner_id": ""}, {"owner_id": "o", "pid": 0},
                       {"owner_id": "o", "ttl_seconds": 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(run_lease.LeaseError) as invalid:
                run_lease.acquire(self.RUN, repo_root=self.records, **kwargs)
            self.assertEqual(invalid.exception.code, "RUN_LEASE_INVALID")
        with self.assertRaises(run_lease.LeaseError) as bad_run:
            run_lease.acquire("../run", "owner", repo_root=self.records)
        self.assertEqual(bad_run.exception.code, "RUN_LEASE_INVALID")

    def test_lock_timeout_is_a_stable_error(self):
        with unittest.mock.patch.object(run_lease, "LOCK_WAIT_SECONDS", 0.1):
            with run_lease._locked(self.RUN, self.records):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    outcome = pool.submit(self._try, "owner-a").result()
        self.assertEqual(outcome, "RUN_LEASE_LOCK_TIMEOUT")


class TakeoverTest(StateTestCase):
    def test_takeover_of_a_stale_lease_is_recorded_in_provenance_first(self):
        run_id = self.start_run()
        run_lease.acquire(run_id, "crashed-owner", pid=dead_pid(), repo_root=self.records)
        result = run_lease.takeover(run_id, "recovery-owner", reason="OPERATOR_RECOVERY", pid=os.getpid(),
                                    repo_root=self.records)
        self.assertEqual(result["acquired"], "takeover")
        recorded = self.parent(run_id)["lease_takeovers"]
        self.assertEqual(len(recorded), 1)
        self.assertEqual((recorded[0]["previous_owner_id"], recorded[0]["owner_id"], recorded[0]["reason"],
                          recorded[0]["stale_reason"], recorded[0]["forced"]),
                         ("crashed-owner", "recovery-owner", "OPERATOR_RECOVERY", "OWNER_PROCESS_DEAD", False))
        self.assertEqual(self.parent(run_id)["owner_id"], "recovery-owner")
        self.assertEqual(result["lease"]["takeovers"], recorded)
        with self.assertRaises(run_lease.LeaseError) as held:
            run_lease.acquire(run_id, "third-owner", repo_root=self.records)
        self.assertEqual(held.exception.code, "RUN_LEASE_HELD")

    def test_expired_lease_of_a_running_holder_needs_a_separate_force_reason(self):
        run_id = self.start_run(owner="sleeper")  # this test process is the running holder
        self.expire(run_id)
        view = run_lease.status(run_id, repo_root=self.records)
        self.assertEqual((view["state"], view["stale_reason"], view["holder_alive"]),
                         ("stale", "HEARTBEAT_EXPIRED", True))
        with self.assertRaises(run_lease.LeaseError) as alive:
            run_lease.takeover(run_id, "recovery", reason="OPERATOR_RECOVERY", repo_root=self.records)
        self.assertEqual(alive.exception.code, "RUN_LEASE_HOLDER_ALIVE")
        self.assertEqual(run_lease.status(run_id, repo_root=self.records)["lease"]["owner_id"], "sleeper")
        forced = run_lease.takeover(run_id, "recovery", reason="OPERATOR_RECOVERY", force_reason="HOLDER_HUNG",
                                    repo_root=self.records)
        entry = forced["takeover"]
        self.assertEqual((entry["forced"], entry["force_reason"]), (True, "HOLDER_HUNG"))
        self.assertEqual(self.parent(run_id)["lease_takeovers"][-1], entry)

    def test_takeover_without_provenance_is_refused_and_leaves_the_lease(self):
        run_lease.acquire("run-missing", "crashed-owner", pid=dead_pid(), repo_root=self.records)
        with self.assertRaises(run_lease.LeaseError) as refused:
            run_lease.takeover("run-missing", "recovery-owner", reason="OPERATOR_RECOVERY", repo_root=self.records)
        self.assertEqual(refused.exception.code, "RUN_LEASE_PROVENANCE_UNAVAILABLE")
        self.assertEqual(run_lease.status("run-missing", repo_root=self.records)["lease"]["owner_id"], "crashed-owner")

    def test_takeover_of_a_terminal_run_is_refused(self):
        run_id = self.start_run()
        po.finish(run_id, "PARTIAL", repo_root=self.records)
        run_lease.acquire(run_id, "crashed-owner", pid=dead_pid(), repo_root=self.records)
        with self.assertRaises(run_lease.LeaseError) as refused:
            run_lease.takeover(run_id, "recovery-owner", reason="OPERATOR_RECOVERY", repo_root=self.records)
        self.assertEqual(refused.exception.code, "RUN_LEASE_PROVENANCE_UNAVAILABLE")

    def test_takeover_requires_a_reason_code_and_an_existing_lease(self):
        run_id = self.start_run()
        with self.assertRaises(run_lease.LeaseError) as missing:
            run_lease.takeover(run_id, "o", reason="OPERATOR_RECOVERY", repo_root=self.records)
        self.assertEqual(missing.exception.code, "RUN_LEASE_NOT_HELD")
        with self.assertRaises(run_lease.LeaseError) as invalid:
            run_lease.takeover(run_id, "o", reason="because I said so", repo_root=self.records)
        self.assertEqual(invalid.exception.code, "RUN_LEASE_INVALID")


class OwnedWriteTest(StateTestCase):
    """Writes to a run are checked against its lease (review finding 1)."""

    EVENT = {"kind": "phase", "phase": "S1", "status": "in_progress"}

    def test_sleep_then_wake_cannot_finish_or_free_the_new_owners_run(self):
        # A owns the run, then sleeps past its TTL; B (another machine's view) takes over.
        run_id = self.start_run(owner="owner-a")
        self.expire(run_id)
        self.rewrite_lease(run_id, host_id="another-machine")
        run_lease.takeover(run_id, "owner-b", reason="OPERATOR_RECOVERY", pid=os.getpid(), repo_root=self.records)
        before = provenance.run_path(self.records, run_id).read_bytes()
        # A wakes up and shuts down: settle, finish and events are all refused.
        for write in (lambda: po.settle(run_id, owner_id="owner-a", repo_root=self.records),
                      lambda: po.finish(run_id, "BLOCKED", owner_id="owner-a", repo_root=self.records),
                      lambda: po.record_event(run_id, self.EVENT, owner_id="owner-a", repo_root=self.records)):
            with self.assertRaises(po.ParentRunError) as refused:
                write()
            self.assertEqual(refused.exception.error_code, "RUN_LEASE_LOST")
        self.assertEqual(provenance.run_path(self.records, run_id).read_bytes(), before)
        view = run_lease.status(run_id, repo_root=self.records)
        self.assertEqual((view["state"], view["lease"]["owner_id"]), ("live", "owner-b"))
        # B keeps working and finishing releases B's own lease.
        po.record_event(run_id, self.EVENT, owner_id="owner-b", repo_root=self.records)
        po.finish(run_id, "PARTIAL", owner_id="owner-b", repo_root=self.records)
        self.assertEqual(run_lease.status(run_id, repo_root=self.records)["state"], "free")

    def test_a_caller_without_the_lease_cannot_write_an_owned_run(self):
        run_id = self.start_run(owner="owner-a")
        for owner in (None, "owner-b"):
            with self.subTest(owner=owner), self.assertRaises(po.ParentRunError) as refused:
                po.finish(run_id, "BLOCKED", owner_id=owner, repo_root=self.records)
            self.assertEqual(refused.exception.error_code, "RUN_LEASE_LOST")
        self.assertEqual(run_lease.status(run_id, repo_root=self.records)["lease"]["owner_id"], "owner-a")
        self.assertEqual(self.parent(run_id)["final_status"], "IN_PROGRESS")

    def test_recorded_owner_may_write_while_the_lease_is_free_and_nobody_else(self):
        run_id = self.start_run(owner="owner-a")
        run_lease.release(run_id, "owner-a", repo_root=self.records)
        with self.assertRaises(po.ParentRunError) as refused:
            po.record_event(run_id, self.EVENT, owner_id="owner-b", repo_root=self.records)
        self.assertEqual(refused.exception.error_code, "RUN_LEASE_NOT_HELD")
        po.record_event(run_id, self.EVENT, owner_id="owner-a", repo_root=self.records)
        # Stale but never taken over: the owner can still record its explicit outcome.
        run_lease.acquire(run_id, "owner-a", pid=os.getpid(), repo_root=self.records)
        self.expire(run_id)
        po.finish(run_id, "PARTIAL", owner_id="owner-a", repo_root=self.records)
        self.assertEqual(run_lease.status(run_id, repo_root=self.records)["state"], "free")

    def test_cli_requires_an_owner_for_every_write(self):
        for command in (["start", "--worktree", str(self.target)], ["event", "run-x"], ["finish", "run-x", "PARTIAL"],
                        ["settle", "run-x"]):
            with self.subTest(command=command[0]):
                result = subprocess.run([sys.executable, str(PARENT_CLI), *command], capture_output=True, text=True,
                                        input="{}")
                self.assertEqual(result.returncode, 2)
                self.assertIn("--owner", result.stderr)


class LeaseCliTest(StateTestCase):
    def cli(self, *args: str) -> tuple[int, dict]:
        env = {**os.environ, provenance.STATE_ENV: str(Path(self._tmp.name) / "state")}
        result = subprocess.run([sys.executable, str(LEASE_CLI), *args], env=env, capture_output=True, text=True)
        return result.returncode, json.loads(result.stdout)

    def test_cli_round_trip_and_stable_codes(self):
        code, acquired = self.cli("acquire", "run-cli", "--owner", "owner-a", "--pid", str(os.getpid()), "--ttl", "60")
        self.assertEqual((code, acquired["ok"], acquired["lease"]["ttl_seconds"]), (0, True, 60))
        code, held = self.cli("acquire", "run-cli", "--owner", "owner-b")
        self.assertEqual((code, held["ok"], held["error_code"]), (1, False, "RUN_LEASE_HELD"))
        self.assertEqual(held["lease"]["owner_id"], "owner-a")
        code, status = self.cli("status", "run-cli")
        self.assertEqual((code, status["state"]), (0, "live"))
        code, beat = self.cli("heartbeat", "run-cli", "--owner", "owner-a")
        self.assertEqual((code, beat["lease"]["pid"]), (0, os.getpid()))
        code, taken = self.cli("takeover", "run-cli", "--owner", "owner-b", "--reason", "OPERATOR_RECOVERY")
        self.assertEqual((code, taken["error_code"]), (1, "RUN_LEASE_HELD"))
        code, released = self.cli("release", "run-cli", "--owner", "owner-a")
        self.assertEqual((code, released["released"]), (0, True))
        self.assertEqual(self.cli("status", "run-cli")[1]["state"], "free")

    def test_cli_default_pid_is_the_calling_process(self):
        code, acquired = self.cli("acquire", "run-cli", "--owner", "owner-a")
        self.assertEqual((code, acquired["lease"]["pid"]), (0, os.getpid()))


class ResumeCheckpointTest(StateTestCase):
    def test_start_and_boundaries_record_the_checkout(self):
        run_id = self.start_run(owner="owner-a")
        parent = self.parent(run_id)
        checkpoint = parent["checkpoint"]
        self.assertEqual(parent["owner_id"], "owner-a")
        self.assertEqual(checkpoint["worktree"], str(self.target.resolve()))
        self.assertEqual(checkpoint["branch"], "main")
        self.assertEqual(checkpoint["head"], git(self.target, "rev-parse", "HEAD"))
        self.assertEqual((checkpoint["phase"], checkpoint["reason"], checkpoint["sequence"]), ("start", "run_start", 1))
        own = {"owner_id": "owner-a", "repo_root": self.records}
        head = self.commit("phase work")
        po.record_event(run_id, {"kind": "phase", "phase": "S1", "status": "in_progress"}, **own)
        checkpoint = self.parent(run_id)["checkpoint"]
        self.assertEqual((checkpoint["phase"], checkpoint["head"], checkpoint["reason"]), ("S1", head, "phase_boundary"))
        before = provenance.run_path(self.records, run_id).read_bytes()
        po.record_event(run_id, {"kind": "phase", "phase": "S1", "status": "in_progress"}, **own)
        self.assertEqual(provenance.run_path(self.records, run_id).read_bytes(), before)
        git(self.target, "switch", "-q", "-c", "integration")
        po.record_event(run_id, {"kind": "checkpoint", "phase": "S1", "reason": "worker_approval"}, **own)
        checkpoint = self.parent(run_id)["checkpoint"]
        self.assertEqual((checkpoint["branch"], checkpoint["reason"]), ("integration", "worker_approval"))
        with self.assertRaises(po.ParentRunError):
            po.record_event(run_id, {"kind": "checkpoint", "phase": "S1", "reason": "rebase"}, **own)
        head = self.commit("integrated")
        po.record_event(run_id, {"kind": "mutation", "code": 1}, **own)
        self.assertEqual(self.parent(run_id)["checkpoint"]["head"], head)

    def resume(self, run_id: str, owner: str = "owner-b", worktree: Path | None = None):
        return po.resume(run_id, worktree=worktree or self.target, owner_id=owner, pid=os.getpid(),
                         explicit_repo=REPO, repo_root=self.records)

    def crashed_run(self) -> str:
        """An owned run whose owner process died: its lease is stale, then recovered by takeover."""
        run_id = self.start_run(owner="crashed")
        run_lease.release(run_id, "crashed", repo_root=self.records)
        return run_id

    def test_verified_resume_takes_the_lease_and_ownership(self):
        run_id = self.crashed_run()
        run_lease.acquire(run_id, "owner-b", pid=os.getpid(), repo_root=self.records)  # e.g. after a takeover
        resumed = self.resume(run_id)
        self.assertEqual(resumed["status"], "IN_PROGRESS")
        self.assertEqual(resumed["lease"]["owner_id"], "owner-b")
        parent = self.parent(run_id)
        self.assertEqual(parent["owner_id"], "owner-b")
        self.assertEqual([(check["status"], check["owner_id"]) for check in parent["resume_checks"]],
                         [("verified", "owner-b")])

    def test_live_lease_blocks_resume_before_any_record_change(self):
        run_id = self.start_run(owner="owner-a")
        before = provenance.run_path(self.records, run_id).read_bytes()
        with self.assertRaises(po.ParentRunError) as held:
            self.resume(run_id)
        self.assertEqual(held.exception.error_code, "RUN_LEASE_HELD")
        self.assertEqual(held.exception.details["lease"]["owner_id"], "owner-a")
        self.assertEqual(provenance.run_path(self.records, run_id).read_bytes(), before)

    def test_stale_lease_blocks_resume_until_an_explicit_takeover(self):
        run_id = self.start_run(owner="crashed")
        self.rewrite_lease(run_id, pid=dead_pid(), pid_start=None)
        with self.assertRaises(po.ParentRunError) as stale:
            self.resume(run_id)
        self.assertEqual(stale.exception.error_code, "RUN_LEASE_STALE")
        self.assertEqual(stale.exception.details["stale_reason"], "OWNER_PROCESS_DEAD")
        run_lease.takeover(run_id, "owner-b", reason="OPERATOR_RECOVERY", pid=os.getpid(), repo_root=self.records)
        self.assertEqual(self.resume(run_id)["status"], "IN_PROGRESS")
        self.assertEqual(len(self.parent(run_id)["lease_takeovers"]), 1)

    def assert_refused_in_progress(self, run_id: str, code: str) -> dict:
        parent = self.parent(run_id)
        self.assertEqual((parent["final_status"], parent["blocker_code"]), ("IN_PROGRESS", None),
                         "a checkpoint failure never closes the run")
        self.assertEqual(parent["resume_checks"][-1]["reason_code"], code)
        self.assertEqual(run_lease.status(run_id, repo_root=self.records)["state"], "free",
                         "the lease taken for the refused resume is released")
        return parent

    def test_changed_head_refuses_resume_and_leaves_the_run_for_an_operator(self):
        run_id = self.crashed_run()
        expected = git(self.target, "rev-parse", "HEAD")
        actual = self.commit("moved while nobody owned the run")
        with self.assertRaises(po.ParentRunError) as drift:
            self.resume(run_id)
        self.assertEqual(drift.exception.error_code, "RESUME_CHECKPOINT_DRIFT")
        self.assertEqual(drift.exception.details["mismatches"],
                         [{"field": "head", "expected": expected, "actual": actual}])
        parent = self.assert_refused_in_progress(run_id, "RESUME_CHECKPOINT_DRIFT")
        self.assertEqual(parent["checkpoint"]["head"], expected, "never silently re-based")
        self.assertEqual(parent["owner_id"], "crashed", "a refused resume does not take ownership")
        # The operator restores the checkout; the same resume then verifies.
        git(self.target, "reset", "-q", "--hard", expected)
        self.assertEqual(self.resume(run_id)["status"], "IN_PROGRESS")

    def test_switched_branch_and_moved_worktree_are_drift(self):
        run_id = self.crashed_run()
        git(self.target, "switch", "-q", "-c", "elsewhere")
        with self.assertRaises(po.ParentRunError) as drift:
            self.resume(run_id)
        self.assertEqual([item["field"] for item in drift.exception.details["mismatches"]], ["branch"])
        git(self.target, "switch", "-q", "main")
        linked = Path(self._tmp.name) / "linked"
        git(self.target, "worktree", "add", "-q", str(linked))
        with self.assertRaises(po.ParentRunError) as moved:
            self.resume(run_id, worktree=linked)
        self.assertEqual(moved.exception.error_code, "RESUME_CHECKPOINT_DRIFT")
        self.assertIn("worktree", [item["field"] for item in moved.exception.details["mismatches"]])
        self.assert_refused_in_progress(run_id, "RESUME_CHECKPOINT_DRIFT")

    def test_interrupted_integration_is_drift_against_the_pre_integration_checkpoint(self):
        run_id = self.start_run(owner="crashed")
        own = {"owner_id": "crashed", "repo_root": self.records}
        po.record_event(run_id, {"kind": "wave", "wave_id": "w1", "task_ids": ["S1"], "status": "declared"}, **own)
        before = git(self.target, "rev-parse", "HEAD")
        po.record_event(run_id, {"kind": "wave", "wave_id": "w1", "task_ids": ["S1"], "status": "integrating"}, **own)
        checkpoint = self.parent(run_id)["checkpoint"]
        self.assertEqual((checkpoint["reason"], checkpoint["head"]), ("integration_started", before))
        merged = self.commit("half of the wave merged, then the process died")
        run_lease.release(run_id, "crashed", repo_root=self.records)
        with self.assertRaises(po.ParentRunError) as drift:
            self.resume(run_id)
        self.assertEqual(drift.exception.error_code, "RESUME_CHECKPOINT_DRIFT")
        self.assertEqual(drift.exception.details["expected"]["head"], before)
        self.assertEqual(drift.exception.details["actual"]["head"], merged)
        self.assertEqual(drift.exception.details["expected"]["reason"], "integration_started")
        self.assert_refused_in_progress(run_id, "RESUME_CHECKPOINT_DRIFT")

    def test_legacy_record_without_checkpoint_loads_but_its_resume_is_refused(self):
        run_id = self.start_run()
        path = provenance.run_path(self.records, run_id)
        record = json.loads(path.read_text())
        del record["parent_orchestration"]["checkpoint"]
        del record["parent_orchestration"]["owner_id"]
        path.write_text(json.dumps(record))
        self.assertEqual(po.status(run_id, repo_root=self.records)["status"], "IN_PROGRESS")
        po.record_event(run_id, {"kind": "phase", "phase": "S1", "status": "in_progress"}, repo_root=self.records)
        self.assertNotIn("checkpoint", self.parent(run_id))
        with self.assertRaises(po.ParentRunError) as missing:
            self.resume(run_id)
        self.assertEqual(missing.exception.error_code, "RESUME_CHECKPOINT_MISSING")
        self.assertEqual(self.assert_refused_in_progress(run_id, "RESUME_CHECKPOINT_MISSING")["resume_checks"][-1]["status"],
                         "unverified")
        # An operator closes it explicitly: take the free lease, then record the outcome.
        run_lease.acquire(run_id, "operator", pid=os.getpid(), repo_root=self.records)
        po.finish(run_id, "BLOCKED", blocker_code="RESUME_CHECKPOINT_MISSING", owner_id="operator",
                  repo_root=self.records)
        self.assertEqual(self.parent(run_id)["final_status"], "BLOCKED")

    def test_run_finished_between_status_and_lease_is_not_resumed(self):
        run_id = self.crashed_run()
        in_progress = po.status(run_id, repo_root=self.records)
        po.finish(run_id, "PARTIAL", owner_id="crashed", repo_root=self.records)
        with unittest.mock.patch.object(po, "status", side_effect=[in_progress, po.status(run_id, repo_root=self.records)]):
            result = self.resume(run_id)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(run_lease.status(run_id, repo_root=self.records)["state"], "free")
        self.assertNotIn("resume_checks", self.parent(run_id))

    def test_explicit_outcome_releases_the_lease(self):
        for finisher in (lambda run_id: po.finish(run_id, "PARTIAL", owner_id="owner-a", repo_root=self.records),
                         lambda run_id: po.settle(run_id, owner_id="owner-a", repo_root=self.records)):
            run_id = self.start_run(owner="owner-a")
            self.assertEqual(run_lease.status(run_id, repo_root=self.records)["state"], "live")
            finisher(run_id)
            self.assertEqual(run_lease.status(run_id, repo_root=self.records)["state"], "free")

    def test_concurrent_resume_from_two_processes_has_one_owner(self):
        state = Path(self._tmp.name) / "state"
        env = {**os.environ, provenance.STATE_ENV: str(state)}
        started = json.loads(subprocess.run(
            [sys.executable, str(PARENT_CLI), "start", "--worktree", str(self.target), "--repo", REPO,
             "--owner", "crashed", "--pid", str(dead_pid())], env=env, capture_output=True, text=True, check=True).stdout)
        run_id = started["run_id"]
        subprocess.run([sys.executable, str(LEASE_CLI), "takeover", run_id, "--owner", "recovery",
                        "--reason", "OPERATOR_RECOVERY", "--pid", str(dead_pid())], env=env, check=True,
                       capture_output=True)
        subprocess.run([sys.executable, str(LEASE_CLI), "release", run_id, "--owner", "recovery"], env=env,
                       check=True, capture_output=True)
        children = [subprocess.Popen([sys.executable, str(PARENT_CLI), "resume", run_id, "--worktree",
                                      str(self.target), "--repo", REPO, "--owner", f"pi-{index}",
                                      "--pid", str(os.getpid())], env=env, stdout=subprocess.PIPE, text=True)
                    for index in range(2)]
        results = [json.loads(child.communicate(timeout=60)[0]) for child in children]
        accepted = [result for result in results if result.get("status") == "IN_PROGRESS"]
        refused = [result for result in results if result.get("ok") is False]
        self.assertEqual(len(accepted), 1, results)
        self.assertEqual([result["error_code"] for result in refused], ["RUN_LEASE_HELD"])
        record = json.loads((state / "runs" / f"{run_id}.json").read_text())
        self.assertEqual([check["owner_id"] for check in record["parent_orchestration"]["resume_checks"]],
                         [accepted[0]["lease"]["owner_id"]])

    def test_drift_cli_reports_expected_and_actual(self):
        state = Path(self._tmp.name) / "state"
        env = {**os.environ, provenance.STATE_ENV: str(state)}
        started = json.loads(subprocess.run(
            [sys.executable, str(PARENT_CLI), "start", "--worktree", str(self.target), "--repo", REPO,
             "--owner", "pi-x", "--pid", str(os.getpid())], env=env, capture_output=True, text=True, check=True).stdout)
        self.commit("drift")
        result = subprocess.run([sys.executable, str(PARENT_CLI), "resume", started["run_id"], "--worktree",
                                 str(self.target), "--repo", REPO, "--owner", "pi-x", "--pid", str(os.getpid())],
                                env=env, capture_output=True, text=True)
        payload = json.loads(result.stdout)
        self.assertEqual((result.returncode, payload["error_code"]), (1, "RESUME_CHECKPOINT_DRIFT"))
        self.assertEqual(payload["details"]["mismatches"][0]["field"], "head")
        self.assertEqual(payload["details"]["actual"]["head"], git(self.target, "rev-parse", "HEAD"))
        self.assertEqual(payload["details"]["run_status"], "IN_PROGRESS")


class WorkerIsolationTest(StateTestCase):
    def test_lease_and_checkpoint_never_touch_the_target_or_widen_worker_scope(self):
        before = session_scope.snapshot(str(self.target))
        run_id = self.start_run(owner="owner-a")
        own = {"owner_id": "owner-a", "repo_root": self.records}
        po.record_event(run_id, {"kind": "phase", "phase": "S1", "status": "in_progress"}, **own)
        po.record_event(run_id, {"kind": "checkpoint", "phase": "S1", "reason": "worker_approval"}, **own)
        run_lease.heartbeat(run_id, "owner-a", repo_root=self.records)
        po.finish(run_id, "PARTIAL", **own)
        self.assertEqual(session_scope.snapshot(str(self.target)), before)
        self.assertEqual(session_scope.worktree(str(self.target), str(self.target))["blocker_code"],
                         "WORKER_WORKSPACE_INVALID")
        linked = Path(self._tmp.name) / "wt-s1"
        git(self.target, "worktree", "add", "-q", "-b", "pico/s1", str(linked))
        self.assertTrue(session_scope.worktree(str(self.target), str(linked))["ok"])

    def test_in_process_scope_owns_its_run(self):
        with po.ParentRun(worktree=self.target, explicit_repo=REPO, repo_root=self.records) as scope:
            scope.event({"kind": "phase", "phase": "S1", "status": "completed"})
            self.assertEqual(run_lease.status(scope.run_id, repo_root=self.records)["lease"]["owner_id"],
                             scope.owner_id)
            with self.assertRaises(po.ParentRunError):
                po.finish(scope.run_id, "BLOCKED", repo_root=self.records)
        self.assertEqual(self.parent(scope.run_id)["final_status"], "BLOCKED")
        self.assertEqual(run_lease.status(scope.run_id, repo_root=self.records)["state"], "free")


class StatusSurfaceTest(StateTestCase):
    def test_garden_shows_who_holds_the_lease(self):
        import watch
        run_id = self.start_run(owner="owner-a")
        runs = provenance.runs_dir(self.records)
        visible = watch.load_runs(runs, REPO)[0]
        self.assertEqual((visible["lease"]["owner_id"], visible["lease"]["state"]), ("owner-a", "live"))
        po.finish(run_id, "PARTIAL", owner_id="owner-a", repo_root=self.records)
        self.assertIsNone(watch.load_runs(runs, REPO)[0]["lease"])


if __name__ == "__main__":
    unittest.main()
