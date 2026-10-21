#!/usr/bin/env python3
"""Deterministic tests for mission store/CLI behavior."""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import yaml
import jsonschema

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import backlog_store as backlog  # noqa: E402
import mission_store as mission_store  # noqa: E402
import mission as mission_cli  # noqa: E402


def make_repo_root(tmp_root: Path) -> Path:
    repo = tmp_root / "repo"
    (repo / "schemas").mkdir(parents=True)
    (repo / "backlog").mkdir(parents=True)
    for schema in ("backlog-item.schema.json", "mission.schema.json", "autonomy.schema.json"):
        shutil.copy(REPO_ROOT / "schemas" / schema, repo / "schemas" / schema)
    return repo


def invoke_cli(args: list[str]) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = mission_cli.main(args)
    return rc, out.getvalue(), err.getvalue()


class MissionStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = make_repo_root(Path(self._tmp.name))
        backlog.backlog_add(
            id="ORCH-001",
            title="Task one",
            description="First task",
            version="v-test",
            priority="high",
            repo_root=self.repo_root,
        )
        backlog.backlog_add(
            id="ORCH-002",
            title="Task two",
            description="Second task",
            version="v-test",
            priority="high",
            repo_root=self.repo_root,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_add_get_list_roundtrip(self):
        mission = mission_store.mission_add(
            id="MISSION-001",
            goal="Ship the feature",
            backlog_items=["ORCH-001"],
            success_criteria=["all tests pass"],
            repo_root=self.repo_root,
        )
        self.assertEqual(mission["status"], "planned")
        self.assertEqual(mission_store.mission_get("MISSION-001", self.repo_root)["goal"], "Ship the feature")
        self.assertEqual([m["id"] for m in mission_store.mission_list(self.repo_root)], ["MISSION-001"])

    def test_schema_rejects_unknown_fields_and_status(self):
        schema = mission_store.load_schema(self.repo_root)
        bad = {
            "id": "MISSION-001",
            "goal": "x",
            "status": "mystery",
            "base_branch": None,
            "base_sha": None,
            "success_criteria": [],
            "backlog_items": [],
            "policy": {"autonomy_profile": "guarded"},
            "budgets": {"max_parallel_tasks": 1, "max_agent_runs": 1, "max_repair_attempts_per_task": 0},
            "current": {"running": [], "waiting_ci": [], "blocked": []},
            "created_at": mission_store.now_iso(),
            "updated_at": mission_store.now_iso(),
            "unexpected": True,
        }
        issues = mission_store.validate_mission_schema(bad, schema)
        self.assertTrue(any("status" in issue for issue in issues))
        self.assertTrue(any("Additional properties" in issue for issue in issues))

    def test_duplicate_id_and_missing_backlog_reference_rejected(self):
        mission_store.mission_add(id="MISSION-001", goal="x", repo_root=self.repo_root)
        with self.assertRaises(mission_store.DuplicateMissionIdError):
            mission_store.mission_add(id="MISSION-001", goal="again", repo_root=self.repo_root)
        with self.assertRaises(mission_store.MissionSchemaValidationError):
            mission_store.mission_add(id="MISSION-002", goal="x", backlog_items=["ORCH-999"], repo_root=self.repo_root)

    def test_allowed_and_forbidden_status_transitions(self):
        mission_store.mission_add(id="MISSION-001", goal="x", repo_root=self.repo_root)
        running = mission_store.mission_update("MISSION-001", self.repo_root, status="running")
        self.assertEqual(running["status"], "running")
        paused = mission_store.mission_update("MISSION-001", self.repo_root, status="paused")
        self.assertEqual(paused["status"], "paused")
        with self.assertRaises(mission_store.MissionTransitionError):
            mission_store.mission_update("MISSION-001", self.repo_root, status="completed")

    def test_empty_mission_does_not_complete(self):
        mission_store.mission_add(id="MISSION-001", goal="x", repo_root=self.repo_root)
        mission_store.mission_update("MISSION-001", self.repo_root, status="running")
        result = mission_store.reconcile_mission("MISSION-001", self.repo_root)
        self.assertFalse(result["changed"])
        self.assertEqual(result["incomplete_backlog_items"], ["<no backlog items>"])
        with self.assertRaises(mission_store.MissionCompletionError):
            mission_store.mission_update("MISSION-001", self.repo_root, status="completed")

    def test_create_completed_applies_completion_guard(self):
        with self.assertRaises(mission_store.MissionCompletionError):
            mission_store.mission_add(
                id="MISSION-001", goal="x", backlog_items=["ORCH-001"], status="completed", repo_root=self.repo_root
            )

    def test_completion_guard_and_reconcile(self):
        mission_store.mission_add(id="MISSION-001", goal="x", backlog_items=["ORCH-001"], repo_root=self.repo_root)
        mission_store.mission_update("MISSION-001", self.repo_root, status="running")
        partial = mission_store.reconcile_mission("MISSION-001", self.repo_root)
        self.assertFalse(partial["changed"])
        self.assertEqual(partial["incomplete_backlog_items"], ["ORCH-001"])

        backlog.backlog_update("ORCH-001", status="planned", repo_root=self.repo_root)
        backlog.backlog_update("ORCH-001", status="ready", repo_root=self.repo_root)
        backlog.backlog_update("ORCH-001", status="in_progress", repo_root=self.repo_root)
        backlog.backlog_update("ORCH-001", status="review", repo_root=self.repo_root)
        backlog.backlog_update("ORCH-001", status="completed", repo_root=self.repo_root)
        done = mission_store.reconcile_mission("MISSION-001", self.repo_root)
        self.assertTrue(done["changed"])
        self.assertEqual(done["mission"]["status"], "completed")

    def test_reconcile_paused_does_not_complete_or_rewrite(self):
        mission_store.mission_add(id="MISSION-001", goal="x", backlog_items=["ORCH-001"], repo_root=self.repo_root)
        mission_store.mission_update("MISSION-001", self.repo_root, status="paused")
        before = mission_store.missions_path(self.repo_root).read_bytes()
        result = mission_store.reconcile_mission("MISSION-001", self.repo_root)
        after = mission_store.missions_path(self.repo_root).read_bytes()
        self.assertFalse(result["changed"])
        self.assertEqual(before, after)

    def test_mission_operations_do_not_mutate_backlog(self):
        before = (self.repo_root / "backlog" / "backlog.yaml").read_bytes()
        mission_store.mission_add(id="MISSION-001", goal="x", backlog_items=["ORCH-001", "ORCH-002"], repo_root=self.repo_root)
        mission_store.mission_update("MISSION-001", self.repo_root, status="running")
        mission_store.reconcile_mission("MISSION-001", self.repo_root)
        after = (self.repo_root / "backlog" / "backlog.yaml").read_bytes()
        self.assertEqual(before, after)

    def test_cli_create_link_show_reconcile_validate(self):
        rc, out, err = invoke_cli([
            "--repo-root", str(self.repo_root),
            "create", "--id", "MISSION-001", "--goal", "Deliver objective",
        ])
        self.assertEqual(rc, 0, err)
        rc, out, err = invoke_cli(["--repo-root", str(self.repo_root), "add-backlog-item", "MISSION-001", "ORCH-001"])
        self.assertEqual(rc, 0, err)
        linked = json.loads(out)
        self.assertEqual(linked["id"], "MISSION-001")
        self.assertEqual(linked["backlog_items"], ["ORCH-001"])
        rc, out, err = invoke_cli(["--repo-root", str(self.repo_root), "show", "MISSION-001"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["goal"], "Deliver objective")
        rc, out, err = invoke_cli(["--repo-root", str(self.repo_root), "validate"])
        self.assertEqual(rc, 0, err)
        self.assertTrue(json.loads(out)["ok"])

    def test_plan_backlog_dag_creates_links_and_is_idempotent(self):
        goal = "Delivery plan:\n - ORCH-001: Existing task\n - ORCH-003: Planner task\n - ORCH-004: Scheduler task"
        mission_store.mission_add(id="MISSION-001", goal=goal, repo_root=self.repo_root)

        result = mission_store.plan_backlog_dag("MISSION-001", self.repo_root, version="v-plan")

        self.assertEqual([item["id"] for item in result["created"]], ["ORCH-003", "ORCH-004"])
        self.assertEqual(result["existing"], ["ORCH-001"])
        self.assertEqual(result["mission"]["backlog_items"], ["ORCH-001", "ORCH-003", "ORCH-004"])
        self.assertEqual(backlog.backlog_get("ORCH-003", self.repo_root)["dependencies"], ["ORCH-001"])
        self.assertEqual(backlog.backlog_get("ORCH-004", self.repo_root)["dependencies"], ["ORCH-003"])
        self.assertIn("bounded engineering task", backlog.backlog_get("ORCH-003", self.repo_root)["description"])

        again = mission_store.plan_backlog_dag("MISSION-001", self.repo_root, version="v-plan")
        self.assertEqual(again["created"], [])
        self.assertEqual(again["mission"]["backlog_items"], ["ORCH-001", "ORCH-003", "ORCH-004"])

    def test_plan_backlog_dag_requires_explicit_backlog_bullets(self):
        mission_store.mission_add(id="MISSION-001", goal="Deliver vaguely", repo_root=self.repo_root)
        with self.assertRaises(mission_store.MissionSchemaValidationError):
            mission_store.plan_backlog_dag("MISSION-001", self.repo_root)

    def test_cli_plan_backlog(self):
        rc, out, err = invoke_cli([
            "--repo-root", str(self.repo_root),
            "create", "--id", "MISSION-001", "--goal", "Plan:\n - ORCH-003: Planner task",
        ])
        self.assertEqual(rc, 0, err)
        rc, out, err = invoke_cli(["--repo-root", str(self.repo_root), "plan-backlog", "MISSION-001", "--version", "v-plan"])
        self.assertEqual(rc, 0, err)
        planned = json.loads(out)
        self.assertEqual([item["id"] for item in planned["created"]], ["ORCH-003"])
        self.assertEqual(planned["mission"]["backlog_items"], ["ORCH-003"])

    def test_schema_fallback_supports_initialized_target_repo_without_vendored_schemas(self):
        bare = Path(self._tmp.name) / "bare-target"
        bare.mkdir()
        rc, out, err = invoke_cli(["--repo-root", str(bare), "create", "--goal", "Deliver objective"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["id"], "MISSION-001")
        self.assertTrue((bare / "missions" / "missions.yaml").exists())

    def test_autonomy_template_validates(self):
        schema = json.loads((REPO_ROOT / "schemas" / "autonomy.schema.json").read_text(encoding="utf-8"))
        data = yaml.safe_load((REPO_ROOT / "templates" / "autonomy.yaml").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema).validate(data)
        self.assertEqual(data["issue_reference"], "refs")

    def test_own_autonomy_policy_closes_issues_and_grants_nothing(self):
        schema = json.loads((REPO_ROOT / "schemas" / "autonomy.schema.json").read_text(encoding="utf-8"))
        data = yaml.safe_load((REPO_ROOT / ".pi" / "autonomy.yaml").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(data)
        self.assertEqual(data["issue_reference"], "closes")
        permissions = data["permissions"]
        self.assertFalse(any(v is True for k, v in permissions.items() if k != "merge"))
        self.assertFalse(permissions["merge"]["enabled"])


if __name__ == "__main__":
    unittest.main()


class ExternalTargetMissionStorageTest(unittest.TestCase):
    """/deliver on an external target never writes into it (audit issue 6)."""

    def setUp(self):
        import os
        import subprocess
        self.tmp = Path(tempfile.mkdtemp(prefix="orch-mission-target-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.target = self.tmp / "examplelab"
        subprocess.run(["git", "init", "-q", str(self.target)], check=True)
        self.worktree = self.tmp / "examplelab-wt"
        subprocess.run(["git", "-C", str(self.target), "-c", "user.name=t", "-c", "user.email=t@e",
                        "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        subprocess.run(["git", "-C", str(self.target), "worktree", "add", "-q", str(self.worktree)], check=True)
        self.state = self.tmp / "state"
        patcher = unittest.mock.patch.dict(os.environ, {"PI_ORCHESTRATOR_STATE_DIR": str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: mission_store.use_missions_file(None))

    def run_cli(self, *args: str) -> int:
        with redirect_stdout(io.StringIO()):
            return mission_cli.main(["--target", str(self.target), *args])

    def test_missions_live_in_state_keyed_by_repository(self):
        self.assertEqual(self.run_cli("create", "--goal", "Ship X"), 0)
        self.assertFalse((self.target / "missions").exists())
        stored = list(self.state.glob("missions/*/missions.yaml"))
        self.assertEqual(len(stored), 1)
        self.assertTrue(stored[0].parent.name.startswith("examplelab-"))
        self.assertEqual(mission_cli.target_missions_file(self.worktree), stored[0],
                         "linked worktrees share their repository's missions")

    def test_legacy_target_local_missions_are_copied_once(self):
        legacy = self.target / "missions" / "missions.yaml"
        legacy.parent.mkdir()
        legacy.write_text("[]\n")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(self.run_cli("list"), 0)
        self.assertIn("Copied legacy target-local missions", stderr.getvalue())
        self.assertTrue(mission_cli.target_missions_file(self.target).exists())
        self.assertTrue(legacy.exists(), "the legacy file is never deleted automatically")
