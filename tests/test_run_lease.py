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
        git(self.target, "remote", "add", "origin", "git@github.com:Examplecorp/examplelab.git")
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
        started = po.start(worktree=self.target, explicit_repo="Examplecorp/examplelab", repo_root=self.records,
                           owner_id=owner, pid=os.getpid() if owner else None)
        self.assertEqual(started["status"], "IN_PROGRESS", started)
        return started["run_id"]

    def parent(self, run_id: str) -> dict:
        return provenance.get_run(self.records, run_id)["parent_orchestration"]


class RunLeaseTest(StateTestCase):
    RUN = "run-lease-test"

    def acquire(self, owner: str, **kwargs):
        return run_lease.acquire(self.RUN, owner, repo_root=self.records, **kwargs)

    def test_lease_records_owner_pid_host_heartbeat_and_expiry_in_state_not_target(self):
        result = self.acquire("owner-a", pid=os.getpid(), ttl_seconds=60, now=1_000_000.0)
        lease = result["lease"]
        self.assertEqual(result["acquired"], "new")
        self.assertEqual((lease["owner_id"], lease["pid"], lease["host"]), ("owner-a", os.getpid(), run_lease.host_name()))
        self.assertEqual(lease["acquired_at"], lease["heartbeat_at"])
        self.assertEqual(run_lease._epoch(lease["expires_at"]) - run_lease._epoch(lease["heartbeat_at"]), 60)
        path = run_lease.lease_path(self.RUN, self.records)
        self.assertEqual(path, self.records / "provenance" / "leases" / f"{self.RUN}.json")
        self.assertEqual(json.loads(path.read_text()), lease)
        self.assertFalse(any(self.target.rglob("*.lock")))
        # With a state directory, leases sit beside the runs there.
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
        self.assertEqual(run_lease.status(self.RUN, repo_root=self.records)["lease"]["owner_id"], "owner-a")
        # A live lease is never taken over either.
        with self.assertRaises(run_lease.LeaseError) as refused:
            run_lease.takeover(self.RUN, "owner-b", reason="OPERATOR_RECOVERY", repo_root=self.records)
        self.assertEqual(refused.exception.code, "RUN_LEASE_HELD")

    def test_same_owner_renews_and_hands_over_to_a_new_pid(self):
        first = self.acquire("owner-a", pid=os.getpid(), now=1_000_000.0)["lease"]
        renewed = self.acquire("owner-a", pid=os.getppid(), now=1_000_030.0)
        self.assertEqual(renewed["acquired"], "renewed")
        self.assertEqual(renewed["lease"]["pid"], os.getppid())
        self.assertEqual(renewed["lease"]["acquired_at"], first["acquired_at"])
        self.assertGreater(run_lease._epoch(renewed["lease"]["expires_at"]), run_lease._epoch(first["expires_at"]))

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
        self.acquire("owner-a", pid=os.getpid(), ttl_seconds=30, now=1_000_000.0)
        view = run_lease.status(self.RUN, repo_root=self.records, now=1_000_029.0)
        self.assertEqual(view["state"], "live")
        view = run_lease.status(self.RUN, repo_root=self.records, now=1_000_030.0)
        self.assertEqual((view["state"], view["stale_reason"]), ("stale", "HEARTBEAT_EXPIRED"))
        with self.assertRaises(run_lease.LeaseError) as stale:
            self.acquire("owner-b", pid=os.getpid(), now=1_000_031.0)
        self.assertEqual(stale.exception.code, "RUN_LEASE_STALE")
        self.assertEqual(stale.exception.stale_reason, "HEARTBEAT_EXPIRED")
        self.assertEqual(run_lease.status(self.RUN, repo_root=self.records)["lease"]["owner_id"], "owner-a")

    def test_dead_pid_on_this_host_is_stale_but_not_on_another_host(self):
        pid = dead_pid()
        self.acquire("owner-a", pid=pid)
        view = run_lease.status(self.RUN, repo_root=self.records)
        self.assertEqual((view["state"], view["stale_reason"]), ("stale", "OWNER_PROCESS_DEAD"))
        path = run_lease.lease_path(self.RUN, self.records)
        lease = json.loads(path.read_text())
        lease["host"] = "another-host"
        path.write_text(json.dumps(lease))
        # A pid on another host cannot be probed: only the heartbeat expiry decides.
        self.assertEqual(run_lease.status(self.RUN, repo_root=self.records)["state"], "live")

    def test_unreadable_lease_is_stale_and_only_takeover_recovers_it(self):
        path = run_lease.lease_path(self.RUN, self.records)
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        view = run_lease.status(self.RUN, repo_root=self.records)
        self.assertEqual((view["state"], view["stale_reason"]), ("stale", "LEASE_UNREADABLE"))
        with self.assertRaises(run_lease.LeaseError) as stale:
            self.acquire("owner-b")
        self.assertEqual(stale.exception.code, "RUN_LEASE_STALE")

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
        self.assertEqual(self.acquire("owner-b", pid=os.getpid())["acquired"], "new")

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
        self.assertEqual(result["lease"]["owner_id"], "recovery-owner")
        recorded = self.parent(run_id)["lease_takeovers"]
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["previous_owner_id"], "crashed-owner")
        self.assertEqual(recorded[0]["owner_id"], "recovery-owner")
        self.assertEqual(recorded[0]["reason"], "OPERATOR_RECOVERY")
        self.assertEqual(recorded[0]["stale_reason"], "OWNER_PROCESS_DEAD")
        self.assertEqual(result["lease"]["takeovers"], recorded)
        # The new lease is live: a third owner is refused.
        with self.assertRaises(run_lease.LeaseError) as held:
            run_lease.acquire(run_id, "third-owner", repo_root=self.records)
        self.assertEqual(held.exception.code, "RUN_LEASE_HELD")

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
        code, status = self.cli("status", "run-cli")
        self.assertEqual(status["state"], "free")

    def test_cli_default_pid_is_the_calling_process(self):
        code, acquired = self.cli("acquire", "run-cli", "--owner", "owner-a")
        self.assertEqual((code, acquired["lease"]["pid"]), (0, os.getpid()))


class ResumeCheckpointTest(StateTestCase):
    def test_start_and_boundaries_record_the_checkout(self):
        run_id = self.start_run(owner="owner-a")
        checkpoint = self.parent(run_id)["checkpoint"]
        self.assertEqual(checkpoint["worktree"], str(self.target.resolve()))
        self.assertEqual(checkpoint["branch"], "main")
        self.assertEqual(checkpoint["head"], git(self.target, "rev-parse", "HEAD"))
        self.assertEqual((checkpoint["phase"], checkpoint["reason"], checkpoint["sequence"]), ("start", "run_start", 1))
        self.assertEqual(run_lease.status(run_id, repo_root=self.records)["lease"]["owner_id"], "owner-a")
        head = self.commit("phase work")
        po.record_event(run_id, {"kind": "phase", "phase": "S1", "status": "in_progress"}, repo_root=self.records)
        checkpoint = self.parent(run_id)["checkpoint"]
        self.assertEqual((checkpoint["phase"], checkpoint["head"], checkpoint["reason"]), ("S1", head, "phase_boundary"))
        before = provenance.run_path(self.records, run_id).read_bytes()
        po.record_event(run_id, {"kind": "phase", "phase": "S1", "status": "in_progress"}, repo_root=self.records)
        self.assertEqual(provenance.run_path(self.records, run_id).read_bytes(), before)
        git(self.target, "switch", "-q", "-c", "integration")
        po.record_event(run_id, {"kind": "checkpoint", "phase": "S1", "reason": "worker_approval"},
                        repo_root=self.records)
        checkpoint = self.parent(run_id)["checkpoint"]
        self.assertEqual((checkpoint["branch"], checkpoint["reason"]), ("integration", "worker_approval"))
        with self.assertRaises(po.ParentRunError):
            po.record_event(run_id, {"kind": "checkpoint", "phase": "S1", "reason": "rebase"}, repo_root=self.records)
        head = self.commit("integrated")
        po.record_event(run_id, {"kind": "mutation", "code": 1}, repo_root=self.records)
        self.assertEqual(self.parent(run_id)["checkpoint"]["head"], head)

    def resume(self, run_id: str, owner: str = "owner-b", pid: int | None = None):
        return po.resume(run_id, worktree=self.target, owner_id=owner, pid=pid or os.getpid(),
                         explicit_repo="Examplecorp/examplelab", repo_root=self.records)

    def test_verified_resume_takes_the_lease_and_records_the_check(self):
        run_id = self.start_run(owner="crashed")
        run_lease.release(run_id, "crashed", repo_root=self.records)
        resumed = self.resume(run_id)
        self.assertEqual(resumed["status"], "IN_PROGRESS")
        self.assertEqual(resumed["lease"]["owner_id"], "owner-b")
        self.assertEqual(resumed["checkpoint"]["head"], git(self.target, "rev-parse", "HEAD"))
        checks = self.parent(run_id)["resume_checks"]
        self.assertEqual([(check["status"], check["owner_id"]) for check in checks], [("verified", "owner-b")])

    def test_live_lease_blocks_resume_before_any_record_change(self):
        run_id = self.start_run(owner="owner-a")
        before = provenance.run_path(self.records, run_id).read_bytes()
        with self.assertRaises(po.ParentRunError) as held:
            self.resume(run_id)
        self.assertEqual(held.exception.error_code, "RUN_LEASE_HELD")
        self.assertEqual(held.exception.details["lease"]["owner_id"], "owner-a")
        self.assertEqual(provenance.run_path(self.records, run_id).read_bytes(), before)

    def test_stale_lease_blocks_resume_until_an_explicit_takeover(self):
        run_id = self.start_run(owner="crashed", )
        run_lease.heartbeat(run_id, "crashed", pid=dead_pid(), repo_root=self.records)
        with self.assertRaises(po.ParentRunError) as stale:
            self.resume(run_id)
        self.assertEqual(stale.exception.error_code, "RUN_LEASE_STALE")
        self.assertEqual(stale.exception.details["stale_reason"], "OWNER_PROCESS_DEAD")
        run_lease.takeover(run_id, "owner-b", reason="OPERATOR_RECOVERY", pid=os.getpid(), repo_root=self.records)
        self.assertEqual(self.resume(run_id)["status"], "IN_PROGRESS")
        self.assertEqual(len(self.parent(run_id)["lease_takeovers"]), 1)

    def test_changed_head_blocks_the_resumed_run_with_expected_and_actual(self):
        run_id = self.start_run(owner="crashed")
        expected = git(self.target, "rev-parse", "HEAD")
        run_lease.release(run_id, "crashed", repo_root=self.records)
        actual = self.commit("moved while nobody owned the run")
        with self.assertRaises(po.ParentRunError) as drift:
            self.resume(run_id)
        self.assertEqual(drift.exception.error_code, "RESUME_CHECKPOINT_DRIFT")
        self.assertEqual(drift.exception.details["mismatches"],
                         [{"field": "head", "expected": expected, "actual": actual}])
        record = provenance.get_run(self.records, run_id)
        parent = record["parent_orchestration"]
        self.assertEqual((parent["final_status"], parent["blocker_code"]), ("BLOCKED", "RESUME_CHECKPOINT_DRIFT"))
        self.assertEqual(parent["resume_checks"][-1]["status"], "drift")
        # Never silently re-based: the checkpoint still names the expected revision.
        self.assertEqual(parent["checkpoint"]["head"], expected)
        self.assertEqual(run_lease.status(run_id, repo_root=self.records)["state"], "free")
        # The blocked run can no longer be resumed or written to.
        self.assertEqual(self.resume(run_id, owner="owner-c")["status"], "BLOCKED")
        with self.assertRaises(po.ParentRunError):
            po.record_event(run_id, {"kind": "phase", "phase": "S1", "status": "in_progress"}, repo_root=self.records)

    def test_switched_branch_and_moved_worktree_are_drift(self):
        run_id = self.start_run(owner="crashed")
        run_lease.release(run_id, "crashed", repo_root=self.records)
        git(self.target, "switch", "-q", "-c", "elsewhere")
        with self.assertRaises(po.ParentRunError) as drift:
            self.resume(run_id)
        self.assertEqual([item["field"] for item in drift.exception.details["mismatches"]], ["branch"])
        run_id = self.start_run(owner="crashed-2")
        run_lease.release(run_id, "crashed-2", repo_root=self.records)
        linked = Path(self._tmp.name) / "linked"
        git(self.target, "worktree", "add", "-q", str(linked))
        with self.assertRaises(po.ParentRunError) as moved:
            po.resume(run_id, worktree=linked, owner_id="owner-b", pid=os.getpid(),
                      explicit_repo="Examplecorp/examplelab", repo_root=self.records)
        self.assertEqual(moved.exception.error_code, "RESUME_CHECKPOINT_DRIFT")
        self.assertIn("worktree", [item["field"] for item in moved.exception.details["mismatches"]])

    def test_legacy_record_without_checkpoint_loads_but_cannot_resume(self):
        run_id = self.start_run()
        path = provenance.run_path(self.records, run_id)
        record = json.loads(path.read_text())
        del record["parent_orchestration"]["checkpoint"]
        path.write_text(json.dumps(record))
        # Older records still load, report status and accept events (no checkpoint is invented).
        self.assertEqual(po.status(run_id, repo_root=self.records)["status"], "IN_PROGRESS")
        po.record_event(run_id, {"kind": "phase", "phase": "S1", "status": "in_progress"}, repo_root=self.records)
        self.assertNotIn("checkpoint", self.parent(run_id))
        with self.assertRaises(po.ParentRunError) as missing:
            self.resume(run_id)
        self.assertEqual(missing.exception.error_code, "RESUME_CHECKPOINT_MISSING")
        parent = self.parent(run_id)
        self.assertEqual((parent["final_status"], parent["blocker_code"]), ("BLOCKED", "RESUME_CHECKPOINT_MISSING"))
        self.assertEqual(parent["resume_checks"][-1]["status"], "unverified")
        self.assertEqual(run_lease.status(run_id, repo_root=self.records)["state"], "free")

    def test_explicit_outcome_releases_the_lease(self):
        for finisher in (lambda run_id: po.finish(run_id, "PARTIAL", repo_root=self.records),
                         lambda run_id: po.settle(run_id, repo_root=self.records)):
            run_id = self.start_run(owner="owner-a")
            self.assertEqual(run_lease.status(run_id, repo_root=self.records)["state"], "live")
            finisher(run_id)
            self.assertEqual(run_lease.status(run_id, repo_root=self.records)["state"], "free")

    def test_concurrent_resume_from_two_processes_has_one_owner(self):
        state = Path(self._tmp.name) / "state"
        env = {**os.environ, provenance.STATE_ENV: str(state)}
        started = json.loads(subprocess.run(
            [sys.executable, str(PARENT_CLI), "start", "--worktree", str(self.target), "--repo",
             "Examplecorp/examplelab", "--owner", "crashed", "--pid", str(dead_pid())],
            env=env, capture_output=True, text=True, check=True).stdout)
        run_id = started["run_id"]
        subprocess.run([sys.executable, str(LEASE_CLI), "takeover", run_id, "--owner", "recovery",
                        "--reason", "OPERATOR_RECOVERY", "--pid", str(dead_pid())], env=env, check=True,
                       capture_output=True)
        # The recovery owner died too; release its lease so both resumers race on a free lease.
        subprocess.run([sys.executable, str(LEASE_CLI), "release", run_id, "--owner", "recovery"], env=env,
                       check=True, capture_output=True)
        children = [subprocess.Popen([sys.executable, str(PARENT_CLI), "resume", run_id, "--worktree",
                                      str(self.target), "--repo", "Examplecorp/examplelab",
                                      "--owner", f"pi-{index}", "--pid", str(os.getpid())],
                                     env=env, stdout=subprocess.PIPE, text=True) for index in range(2)]
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
            [sys.executable, str(PARENT_CLI), "start", "--worktree", str(self.target), "--repo",
             "Examplecorp/examplelab"], env=env, capture_output=True, text=True, check=True).stdout)
        self.commit("drift")
        result = subprocess.run([sys.executable, str(PARENT_CLI), "resume", started["run_id"], "--worktree",
                                 str(self.target), "--repo", "Examplecorp/examplelab", "--owner", "pi-x"],
                                env=env, capture_output=True, text=True)
        payload = json.loads(result.stdout)
        self.assertEqual((result.returncode, payload["error_code"]), (1, "RESUME_CHECKPOINT_DRIFT"))
        self.assertEqual(payload["details"]["mismatches"][0]["field"], "head")
        self.assertEqual(payload["details"]["actual"]["head"], git(self.target, "rev-parse", "HEAD"))


class WorkerIsolationTest(StateTestCase):
    def test_lease_and_checkpoint_never_touch_the_target_or_widen_worker_scope(self):
        before = session_scope.snapshot(str(self.target))
        run_id = self.start_run(owner="owner-a")
        po.record_event(run_id, {"kind": "phase", "phase": "S1", "status": "in_progress"}, repo_root=self.records)
        po.record_event(run_id, {"kind": "checkpoint", "phase": "S1", "reason": "worker_approval"},
                        repo_root=self.records)
        run_lease.heartbeat(run_id, "owner-a", repo_root=self.records)
        po.finish(run_id, "PARTIAL", repo_root=self.records)
        self.assertEqual(session_scope.snapshot(str(self.target)), before)
        # The parent checkout is still not a valid worker workspace; a linked worktree still is.
        self.assertEqual(session_scope.worktree(str(self.target), str(self.target))["blocker_code"],
                         "WORKER_WORKSPACE_INVALID")
        linked = Path(self._tmp.name) / "wt-s1"
        git(self.target, "worktree", "add", "-q", "-b", "pico/s1", str(linked))
        self.assertTrue(session_scope.worktree(str(self.target), str(linked))["ok"])


class StatusSurfaceTest(StateTestCase):
    def test_garden_shows_who_holds_the_lease(self):
        import watch
        run_id = self.start_run(owner="owner-a")
        runs = provenance.runs_dir(self.records)
        visible = watch.load_runs(runs, "Examplecorp/examplelab")[0]
        self.assertEqual(visible["lease"]["owner_id"], "owner-a")
        self.assertEqual(visible["lease"]["state"], "live")
        po.finish(run_id, "PARTIAL", repo_root=self.records)
        self.assertIsNone(watch.load_runs(runs, "Examplecorp/examplelab")[0]["lease"])


if __name__ == "__main__":
    unittest.main()
