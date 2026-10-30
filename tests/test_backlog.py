#!/usr/bin/env python3
"""Deterministic tests for tools/backlog_store.py.

Run with: python3 -m unittest discover -s tests -p 'test_*.py'

Every test operates against an isolated temporary repo root (a copy of the
real schemas/backlog-item.schema.json under a fresh backlog/ directory), so
no test ever reads or writes this repository's real backlog/backlog.yaml.
"""

from __future__ import annotations

import io
import concurrent.futures
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import backlog_store as store  # noqa: E402
import backlog as backlog_cli  # noqa: E402
import provenance  # noqa: E402


def make_repo_root(tmp_root: Path) -> Path:
    repo = tmp_root / "repo"
    (repo / "schemas").mkdir(parents=True)
    (repo / "backlog").mkdir(parents=True)
    shutil.copy(
        REPO_ROOT / "schemas" / "backlog-item.schema.json",
        repo / "schemas" / "backlog-item.schema.json",
    )
    return repo


class BacklogStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = make_repo_root(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    # -- basic add/list/get -------------------------------------------------

    def test_add_then_get_roundtrip(self):
        item = store.backlog_add(
            title="Example",
            description="An example item.",
            version="v9.9",
            priority="medium",
            acceptance_criteria=["it exists"],
            repo_root=self.repo_root,
        )
        self.assertEqual(item["id"], "ORCH-001")
        self.assertEqual(item["status"], "proposed")
        fetched = store.backlog_get("ORCH-001", self.repo_root)
        self.assertEqual(fetched, item)

    def test_concurrent_additions_keep_both_items(self):
        first_read = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        second_read = threading.Event()
        original = store.load_backlog

        def slow_first_read(repo_root):
            items = original(repo_root)
            if threading.current_thread().name == "ORCH-001":
                first_read.set()
                self.assertTrue(release_first.wait(5))
            elif threading.current_thread().name == "ORCH-002":
                second_read.set()
            return items

        def add(item_id):
            threading.current_thread().name = item_id
            if item_id == "ORCH-002":
                second_started.set()
            return store.backlog_add(id=item_id, title=item_id, description="test",
                                     version="v1", repo_root=self.repo_root)

        with patch.object(store, "load_backlog", slow_first_read):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(add, "ORCH-001")
                self.assertTrue(first_read.wait(5))
                second = pool.submit(add, "ORCH-002")
                try:
                    self.assertTrue(second_started.wait(5))
                    self.assertFalse(second_read.wait(0.1), "second writer read before the first committed")
                finally:
                    release_first.set()
                self.assertEqual({first.result(timeout=5)["id"], second.result(timeout=5)["id"]},
                                 {"ORCH-001", "ORCH-002"})
        self.assertEqual({item["id"] for item in original(self.repo_root)}, {"ORCH-001", "ORCH-002"})

    def test_backlog_lock_blocks_another_process_without_dirtying_repo(self):
        script = (
            "import sys; sys.path.insert(0, sys.argv[1]); import backlog_store; "
            "from pathlib import Path; print('ready', flush=True); "
            "backlog_store.backlog_add(id='ORCH-001', title='one', description='test', "
            "version='v1', repo_root=Path(sys.argv[2]))"
        )
        with store.locked_backlog(self.repo_root):
            child = subprocess.Popen([sys.executable, "-c", script, str(TOOLS_DIR), str(self.repo_root)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(child.stdout.readline().strip(), "ready")
            with self.assertRaises(subprocess.TimeoutExpired):
                child.wait(timeout=0.2)
            self.assertEqual(store.load_backlog(self.repo_root), [])
        _, stderr = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, stderr)
        self.assertEqual([item["id"] for item in store.load_backlog(self.repo_root)], ["ORCH-001"])
        self.assertFalse(any(self.repo_root.rglob("*.lock")))

    def test_generate_next_id_is_monotonic_and_gap_aware(self):
        store.backlog_add(title="A", description="d", version="v1", id="ORCH-001", repo_root=self.repo_root)
        store.backlog_add(title="B", description="d", version="v1", id="ORCH-005", repo_root=self.repo_root)
        third = store.backlog_add(title="C", description="d", version="v1", repo_root=self.repo_root)
        self.assertEqual(third["id"], "ORCH-006")

    def test_get_missing_id_raises_not_found(self):
        with self.assertRaises(store.NotFoundError):
            store.backlog_get("ORCH-999", self.repo_root)

    def test_list_filters_by_status_and_version(self):
        store.backlog_add(title="A", description="d", version="v1", id="ORCH-001", repo_root=self.repo_root)
        store.backlog_add(title="B", description="d", version="v2", id="ORCH-002", repo_root=self.repo_root)
        store.backlog_update("ORCH-002", status="planned", repo_root=self.repo_root)
        self.assertEqual([i["id"] for i in store.backlog_list(self.repo_root, version="v2")], ["ORCH-002"])
        self.assertEqual([i["id"] for i in store.backlog_list(self.repo_root, status="planned")], ["ORCH-002"])
        self.assertEqual([i["id"] for i in store.backlog_list(self.repo_root, status="proposed")], ["ORCH-001"])

    # -- schema validation ----------------------------------------------------

    def test_schema_rejects_unknown_status(self):
        schema = store.load_schema(self.repo_root)
        bad_item = {
            "id": "ORCH-001",
            "title": "x",
            "description": "x",
            "status": "not_a_real_status",
            "version": "v1",
            "priority": "low",
            "dependencies": [],
            "created_at": store.now_iso(),
            "updated_at": store.now_iso(),
            "acceptance_criteria": [],
        }
        issues = store.validate_item_schema(bad_item, schema)
        self.assertTrue(issues)

    def test_schema_rejects_additional_properties(self):
        schema = store.load_schema(self.repo_root)
        item = {
            "id": "ORCH-001",
            "title": "x",
            "description": "x",
            "status": "proposed",
            "version": "v1",
            "priority": "low",
            "dependencies": [],
            "created_at": store.now_iso(),
            "updated_at": store.now_iso(),
            "acceptance_criteria": [],
            "unexpected_field": "nope",
        }
        issues = store.validate_item_schema(item, schema)
        self.assertTrue(issues)

    def test_github_pr_and_sync_schema_roundtrip(self):
        item = store.backlog_add(
            title="x", description="d", version="v1", id="ORCH-001",
            github_issue=5, github_pr=8, repo_root=self.repo_root,
        )
        self.assertEqual(item["github_pr"], 8)
        updated = store.backlog_update(
            "ORCH-001",
            github_pr_sync={
                "last_synced_at": "2026-01-01T00:00:00Z",
                "repo": "acme/repo",
                "pr": 8,
                "issue": 5,
                "remote_title": "x",
                "remote_state": "open",
                "remote_body": "Refs #5",
                "head_ref": "feature",
                "base_ref": "main",
                "is_draft": False,
            },
            repo_root=self.repo_root,
        )
        self.assertEqual(updated["github_pr_sync"]["pr"], 8)
        self.assertEqual(store.validate_backlog_collection(store.load_backlog(self.repo_root), store.load_schema(self.repo_root)), [])

    def test_github_pr_sync_must_match_github_pr_and_issue(self):
        schema = store.load_schema(self.repo_root)
        base = {
            "id": "ORCH-001",
            "title": "x",
            "description": "x",
            "status": "proposed",
            "version": "v1",
            "priority": "low",
            "dependencies": [],
            "created_at": store.now_iso(),
            "updated_at": store.now_iso(),
            "acceptance_criteria": [],
            "github_issue": 5,
            "github_pr": 8,
            "github_pr_sync": {
                "last_synced_at": "2026-01-01T00:00:00Z",
                "repo": "acme/repo",
                "pr": 9,
                "issue": 6,
                "remote_title": "x",
                "remote_state": "open",
                "remote_body": "Refs #5",
                "head_ref": "feature",
                "base_ref": "main",
                "is_draft": False,
            },
        }
        issues = store.validate_backlog_collection([base], schema)
        self.assertTrue(any("github_pr_sync PR does not match" in i for i in issues))
        self.assertTrue(any("github_pr_sync issue does not match" in i for i in issues))

    def test_github_pr_requires_github_issue(self):
        schema = store.load_schema(self.repo_root)
        item = {
            "id": "ORCH-001",
            "title": "x",
            "description": "x",
            "status": "proposed",
            "version": "v1",
            "priority": "low",
            "dependencies": [],
            "created_at": store.now_iso(),
            "updated_at": store.now_iso(),
            "acceptance_criteria": [],
            "github_pr": 8,
        }
        issues = store.validate_backlog_collection([item], schema)
        self.assertTrue(any("github_pr requires github_issue" in i for i in issues))

    # -- unique id enforcement -------------------------------------------------

    def test_duplicate_id_rejected(self):
        store.backlog_add(title="A", description="d", version="v1", id="ORCH-001", repo_root=self.repo_root)
        with self.assertRaises(store.DuplicateIdError):
            store.backlog_add(title="B", description="d", version="v1", id="ORCH-001", repo_root=self.repo_root)

    # -- dependency validation / cycles -----------------------------------------

    def test_dependency_must_reference_existing_item(self):
        schema = store.load_schema(self.repo_root)
        items = [
            {
                "id": "ORCH-001",
                "title": "x",
                "description": "x",
                "status": "proposed",
                "version": "v1",
                "priority": "low",
                "dependencies": ["ORCH-999"],
                "created_at": store.now_iso(),
                "updated_at": store.now_iso(),
                "acceptance_criteria": [],
            }
        ]
        issues = store.validate_backlog_collection(items, schema)
        self.assertTrue(any("does not reference an existing backlog item" in i for i in issues))

    def test_circular_dependency_detected(self):
        schema = store.load_schema(self.repo_root)
        base = {
            "title": "x",
            "description": "x",
            "status": "proposed",
            "version": "v1",
            "priority": "low",
            "created_at": store.now_iso(),
            "updated_at": store.now_iso(),
            "acceptance_criteria": [],
        }
        items = [
            {**base, "id": "ORCH-001", "dependencies": ["ORCH-002"]},
            {**base, "id": "ORCH-002", "dependencies": ["ORCH-003"]},
            {**base, "id": "ORCH-003", "dependencies": ["ORCH-001"]},
        ]
        issues = store.validate_backlog_collection(items, schema)
        self.assertTrue(any("circular dependency" in i for i in issues))

    def test_no_false_positive_cycle_on_diamond_dependency(self):
        schema = store.load_schema(self.repo_root)
        base = {
            "title": "x",
            "description": "x",
            "status": "proposed",
            "version": "v1",
            "priority": "low",
            "created_at": store.now_iso(),
            "updated_at": store.now_iso(),
            "acceptance_criteria": [],
        }
        items = [
            {**base, "id": "ORCH-001", "dependencies": []},
            {**base, "id": "ORCH-002", "dependencies": ["ORCH-001"]},
            {**base, "id": "ORCH-003", "dependencies": ["ORCH-001"]},
            {**base, "id": "ORCH-004", "dependencies": ["ORCH-002", "ORCH-003"]},
        ]
        issues = store.validate_backlog_collection(items, schema)
        self.assertEqual(issues, [])

    def test_add_rejects_self_dependency_cycle(self):
        # A single-item self-dependency is both a "references existing item"
        # violation avoider and a trivial 1-cycle; adding it directly must fail.
        schema = store.load_schema(self.repo_root)
        item = {
            "id": "ORCH-001",
            "title": "x",
            "description": "x",
            "status": "proposed",
            "version": "v1",
            "priority": "low",
            "dependencies": ["ORCH-001"],
            "created_at": store.now_iso(),
            "updated_at": store.now_iso(),
            "acceptance_criteria": [],
        }
        issues = store.validate_backlog_collection([item], schema)
        self.assertTrue(any("cannot depend on itself" in i for i in issues))

    # -- state transitions -------------------------------------------------

    def test_valid_transition_chain(self):
        store.backlog_add(title="A", description="d", version="v1", id="ORCH-001", repo_root=self.repo_root)
        for target in ("planned", "ready", "in_progress", "review", "completed"):
            item = store.backlog_update("ORCH-001", status=target, repo_root=self.repo_root)
            self.assertEqual(item["status"], target)

    def test_invalid_transition_rejected(self):
        store.backlog_add(title="A", description="d", version="v1", id="ORCH-001", status="proposed", repo_root=self.repo_root)
        with self.assertRaises(store.TransitionError):
            store.backlog_update("ORCH-001", status="in_progress", repo_root=self.repo_root)

    def test_terminal_states_have_no_outgoing_transitions(self):
        store.backlog_add(title="A", description="d", version="v1", id="ORCH-001", status="proposed", repo_root=self.repo_root)
        store.backlog_update("ORCH-001", status="cancelled", repo_root=self.repo_root)
        with self.assertRaises(store.TransitionError):
            store.backlog_update("ORCH-001", status="planned", repo_root=self.repo_root)

    def test_no_op_transition_rejected(self):
        store.backlog_add(title="A", description="d", version="v1", id="ORCH-001", status="proposed", repo_root=self.repo_root)
        with self.assertRaises(store.TransitionError):
            store.backlog_update("ORCH-001", status="proposed", repo_root=self.repo_root)

    def test_blocked_requires_reason(self):
        store.backlog_add(title="A", description="d", version="v1", id="ORCH-001", status="proposed", repo_root=self.repo_root)
        store.backlog_update("ORCH-001", status="planned", repo_root=self.repo_root)
        with self.assertRaises(store.TransitionError):
            store.backlog_update("ORCH-001", status="blocked", repo_root=self.repo_root)

    def test_blocked_returns_to_prior_state(self):
        store.backlog_add(title="A", description="d", version="v1", id="ORCH-001", status="proposed", repo_root=self.repo_root)
        store.backlog_update("ORCH-001", status="planned", repo_root=self.repo_root)
        blocked = store.backlog_update(
            "ORCH-001", status="blocked", blocked_reason="waiting on external decision", repo_root=self.repo_root
        )
        self.assertEqual(blocked["blocked_from"], "planned")
        unblocked = store.backlog_update("ORCH-001", status="planned", repo_root=self.repo_root)
        self.assertEqual(unblocked["status"], "planned")
        self.assertIsNone(unblocked.get("blocked_from"))
        self.assertIsNone(unblocked.get("blocked_reason"))

    def test_blocked_from_in_progress_can_return_to_in_progress(self):
        store.backlog_add(title="A", description="d", version="v1", id="ORCH-001", status="proposed", repo_root=self.repo_root)
        for target in ("planned", "ready", "in_progress"):
            store.backlog_update("ORCH-001", status=target, repo_root=self.repo_root)
        store.backlog_update("ORCH-001", status="blocked", blocked_reason="waiting", repo_root=self.repo_root)
        item = store.backlog_update("ORCH-001", status="in_progress", repo_root=self.repo_root)
        self.assertEqual(item["status"], "in_progress")

    # -- dependency-gated statuses ---------------------------------------------

    def test_ready_requires_dependencies_completed(self):
        store.backlog_add(title="A", description="d", version="v1", id="ORCH-001", status="proposed", repo_root=self.repo_root)
        store.backlog_add(
            title="B", description="d", version="v1", id="ORCH-002", status="proposed",
            dependencies=["ORCH-001"], repo_root=self.repo_root,
        )
        store.backlog_update("ORCH-002", status="planned", repo_root=self.repo_root)
        with self.assertRaises(store.DependencyNotCompletedError):
            store.backlog_update("ORCH-002", status="ready", repo_root=self.repo_root)

    def test_ready_allowed_once_dependency_completed(self):
        store.backlog_add(title="A", description="d", version="v1", id="ORCH-001", status="proposed", repo_root=self.repo_root)
        store.backlog_add(
            title="B", description="d", version="v1", id="ORCH-002", status="proposed",
            dependencies=["ORCH-001"], repo_root=self.repo_root,
        )
        for target in ("planned", "ready", "in_progress", "review", "completed"):
            store.backlog_update("ORCH-001", status=target, repo_root=self.repo_root)
        store.backlog_update("ORCH-002", status="planned", repo_root=self.repo_root)
        item = store.backlog_update("ORCH-002", status="ready", repo_root=self.repo_root)
        self.assertEqual(item["status"], "ready")

    def test_completed_rejected_when_dependency_not_completed_direct(self):
        store.backlog_add(title="A", description="d", version="v1", id="ORCH-001", status="proposed", repo_root=self.repo_root)
        store.backlog_add(
            title="B", description="d", version="v1", id="ORCH-002", status="proposed",
            dependencies=["ORCH-001"], repo_root=self.repo_root,
        )
        store.backlog_update("ORCH-002", status="planned", repo_root=self.repo_root)
        # ORCH-001 (the dependency) is still 'proposed', so entering the
        # dependency-gated 'ready' status (a prerequisite for eventually
        # reaching 'completed') must be rejected.
        items = store.load_backlog(self.repo_root)
        items_by_id = {i["id"]: i for i in items}
        b = items_by_id["ORCH-002"]
        with self.assertRaises(store.DependencyNotCompletedError):
            store.validate_transition(b, "ready", items_by_id)

    # -- reporting -----------------------------------------------------------

    def test_backlog_next_ranks_by_priority_then_id(self):
        store.backlog_add(title="Low", description="d", version="v1", id="ORCH-001", status="proposed", priority="low", repo_root=self.repo_root)
        store.backlog_add(title="Crit", description="d", version="v1", id="ORCH-002", status="proposed", priority="critical", repo_root=self.repo_root)
        store.backlog_add(title="High", description="d", version="v1", id="ORCH-003", status="proposed", priority="high", repo_root=self.repo_root)
        for id_ in ("ORCH-001", "ORCH-002", "ORCH-003"):
            store.backlog_update(id_, status="planned", repo_root=self.repo_root)
            store.backlog_update(id_, status="ready", repo_root=self.repo_root)
        ranked = [i["id"] for i in store.backlog_next(self.repo_root)]
        self.assertEqual(ranked, ["ORCH-002", "ORCH-003", "ORCH-001"])

    def test_backlog_report_core_groups_by_status_and_version(self):
        store.backlog_add(title="A", description="d", version="v1", id="ORCH-001", status="proposed", repo_root=self.repo_root)
        store.backlog_update("ORCH-001", status="planned", repo_root=self.repo_root)
        store.backlog_update(
            "ORCH-001", status="blocked", blocked_reason="waiting on external input", repo_root=self.repo_root
        )
        report = store.backlog_report_core(self.repo_root).as_dict()
        self.assertEqual(report["blocked"], [{"id": "ORCH-001", "reason": "waiting on external input", "blocked_from": "planned"}])
        self.assertEqual(report["in_progress"], [])
        self.assertEqual(report["completed_by_version"], {})

    def test_github_issue_association_round_trips(self):
        store.backlog_add(title="A", description="d", version="v1", id="ORCH-001", status="proposed", repo_root=self.repo_root)
        item = store.backlog_update("ORCH-001", github_issue=42, repo_root=self.repo_root)
        self.assertEqual(item["github_issue"], 42)
        report = store.backlog_report_core(self.repo_root).as_dict()
        self.assertEqual(report["github_issues"]["ORCH-001"], 42)

    def test_save_backlog_is_stable_sorted_by_id(self):
        store.backlog_add(title="C", description="d", version="v1", id="ORCH-003", repo_root=self.repo_root)
        store.backlog_add(title="A", description="d", version="v1", id="ORCH-001", repo_root=self.repo_root)
        store.backlog_add(title="B", description="d", version="v1", id="ORCH-002", repo_root=self.repo_root)
        ids = [i["id"] for i in store.load_backlog(self.repo_root)]
        self.assertEqual(ids, ["ORCH-001", "ORCH-002", "ORCH-003"])

    def test_malformed_backlog_timestamp_rejected(self):
        schema = store.load_schema(self.repo_root)
        item = {
            "id": "ORCH-001", "title": "x", "description": "x", "status": "proposed",
            "version": "v1", "priority": "low", "dependencies": [],
            "created_at": "garbage", "updated_at": store.now_iso(), "acceptance_criteria": [],
        }
        self.assertTrue(store.validate_item_schema(item, schema))

    def test_blocked_metadata_rejected_on_non_blocked_item(self):
        schema = store.load_schema(self.repo_root)
        item = {
            "id": "ORCH-001", "title": "x", "description": "x", "status": "planned",
            "version": "v1", "priority": "low", "dependencies": [],
            "created_at": store.now_iso(), "updated_at": store.now_iso(), "acceptance_criteria": [],
            "blocked_from": "planned", "blocked_reason": "stale",
        }
        issues = store.validate_backlog_collection([item], schema)
        self.assertTrue(any("only permitted while status is 'blocked'" in i for i in issues))

    def test_duplicate_github_issue_mapping_rejected(self):
        store.backlog_add(title="A", description="d", version="v1", id="ORCH-001", github_issue=5, repo_root=self.repo_root)
        with self.assertRaises(store.SchemaValidationError):
            store.backlog_add(title="B", description="d", version="v1", id="ORCH-002", github_issue=5, repo_root=self.repo_root)

    def test_changing_github_issue_with_sync_baseline_rejected(self):
        store.backlog_add(title="A", description="d", version="v1", id="ORCH-001", github_issue=5, repo_root=self.repo_root)
        store.backlog_update("ORCH-001", github_sync={
            "last_synced_at": store.now_iso(), "repo": "acme/repo", "issue": 5,
            "remote_title": "A", "remote_state": "open", "remote_labels": [],
        }, repo_root=self.repo_root)
        with self.assertRaises(store.TransitionError):
            store.backlog_update("ORCH-001", github_issue=6, repo_root=self.repo_root)


# ---------------------------------------------------------------------------
# tools/backlog.py `trace` CLI: render an orchestration run trace.
# ---------------------------------------------------------------------------

def make_trace_repo_root(tmp_root: Path) -> Path:
    """Like make_repo_root, plus a provenance/runs/ dir and the real
    run-record schema, since `trace` joins backlog and provenance state."""
    repo = tmp_root / "repo"
    (repo / "schemas").mkdir(parents=True)
    (repo / "backlog").mkdir(parents=True)
    (repo / "provenance" / "runs").mkdir(parents=True)
    shutil.copy(
        REPO_ROOT / "schemas" / "backlog-item.schema.json",
        repo / "schemas" / "backlog-item.schema.json",
    )
    shutil.copy(
        REPO_ROOT / "schemas" / "run-record.schema.json",
        repo / "schemas" / "run-record.schema.json",
    )
    return repo


class BacklogTraceCliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = make_trace_repo_root(Path(self._tmp.name))
        self._repo_root_patch = patch.object(backlog_cli, "REPO_ROOT", self.repo_root)
        self._repo_root_patch.start()

    def tearDown(self):
        self._repo_root_patch.stop()
        self._tmp.cleanup()

    def _run_trace(self, argv: list[str]) -> tuple[int, str, str]:
        parser = backlog_cli.build_parser()
        args = parser.parse_args(["trace", *argv])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = args.func(args)
        return code, out.getvalue(), err.getvalue()

    def test_no_selector_is_an_error(self):
        code, _, err = self._run_trace([])
        self.assertEqual(code, 1)
        self.assertIn("specify RUN_ID", err)

    def test_run_id_and_backlog_id_together_is_an_error(self):
        code, _, err = self._run_trace(["some-run", "--backlog-id", "ORCH-001"])
        self.assertEqual(code, 1)
        self.assertIn("mutually exclusive", err)

    def test_run_id_and_latest_together_is_an_error(self):
        code, _, err = self._run_trace(["some-run", "--latest"])
        self.assertEqual(code, 1)
        self.assertIn("mutually exclusive", err)

    def test_unknown_run_id_is_reported_not_raised(self):
        code, _, err = self._run_trace(["does-not-exist"])
        self.assertEqual(code, 1)
        self.assertIn("no run record for run_id", err)

    def test_no_matching_backlog_id_is_reported(self):
        code, _, err = self._run_trace(["--backlog-id", "ORCH-999"])
        self.assertEqual(code, 1)
        self.assertIn("no matching run found", err)

    def _seed_run(self) -> None:
        provenance.start_run(
            self.repo_root, run_id="run-cli-1", backlog_id="ORCH-011", github_issue=11,
            user_objective="Implement end-of-run orchestration trace.",
            route_task_result={"ok": True, "selected_route": "CLAUDE", "policy_version": 4,
                                "fallback_applied": False, "matches": {"base": ["CLAUDE"], "effective": ["CLAUDE"]},
                                "execution_plan": {"schema_version": 3, "independent_review_required": False, "steps": [
                                    {"id": "claude_worker", "order": 1, "role": "worker", "family": "claude",
                                     "intent": "implementation", "capacity_tier": "standard", "reasoning_effort": "medium",
                                     "context_strategy": "focused", "fresh_context": True, "mutation_permission": "allowed",
                                     "depends_on": [], "independent_review": False},
                                ]}},
            starting_commit="c0",
        )
        provenance.add_step(
            self.repo_root, "run-cli-1", task_id="claude_worker", execution_plan_step_id="claude_worker",
            role="worker", family="claude", status="completed", outcome="accepted",
        )
        provenance.finish_run(self.repo_root, "run-cli-1", outcome="completed", ending_commit="c1")

    def test_trace_by_run_id_text_output(self):
        self._seed_run()
        code, out, _ = self._run_trace(["run-cli-1"])
        self.assertEqual(code, 0)
        self.assertIn("Run: run-cli-1", out)
        self.assertIn("Trace: provenance/runs/run-cli-1.json", out)
        self.assertIn("References: backlog ORCH-011; GitHub issue #11", out)
        self.assertIn("Selected: CLAUDE", out)
        self.assertIn("Final status: completed", out)

    def test_trace_by_backlog_id_latest_json_output(self):
        self._seed_run()
        code, out, _ = self._run_trace(["--backlog-id", "ORCH-011", "--latest", "--json"])
        self.assertEqual(code, 0)
        import json
        payload = json.loads(out)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(len(payload["runs"]), 1)
        self.assertEqual(payload["runs"][0]["run_id"], "run-cli-1")
        self.assertEqual(payload["runs"][0]["route"]["selected_route"], "CLAUDE")

    def test_trace_json_output_never_contains_absolute_repo_path(self):
        self._seed_run()
        _, out, _ = self._run_trace(["run-cli-1", "--json"])
        self.assertNotIn(str(self.repo_root), out)


if __name__ == "__main__":
    unittest.main()
