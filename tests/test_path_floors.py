#!/usr/bin/env python3
"""Path-triggered risk floors (#76): `.pi/routing.yaml` `path_floors` applied by
route_task from a task's files_or_areas, recomputed by the route-result
validator, and checked against a worker's real diff before integration.

Deterministic: no model, no network, no GitHub. Git runs only on temporary
repositories."""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import parent_orchestration as po  # noqa: E402
import provenance  # noqa: E402
import routing_engine as engine  # noqa: E402

PROJECT = {
    "path_floors": [
        {"paths": ["migrations/**", "schemas/**"], "floors": {"failure_cost": "high", "behavior_preservation": "required"}},
        {"paths": [".github/workflows/**", "**/*secret*"], "floors": {"security_sensitive": True}},
    ],
}


def small_task(**changes) -> dict:
    """A small, unflagged config edit: routed CLAUDE with a light worker."""
    case = next(c for c in engine.load_yaml(ROOT / "tests/routing-cases.yaml")["cases"] if c["id"] == "case-17-config-edit")
    task = copy.deepcopy(case["task"])
    task.update(changes)
    return task


def run_route_task(task: dict, project: dict | None, tmp: Path) -> dict:
    args = [sys.executable, str(ROOT / "tools/route_task.py")]
    if project is not None:
        config = tmp / "routing.yaml"
        config.write_text(json.dumps(project))
        args += ["--project-routing", str(config)]
    done = subprocess.run(args, input=json.dumps({"classified_task": task}), capture_output=True, text=True,
                          check=True, timeout=60)
    return json.loads(done.stdout)


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


class PathFloorRoutingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets = engine.load_policy_assets(ROOT)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def route(self, task: dict, project: dict | None = PROJECT) -> dict:
        resolution = engine.resolve_route(task, self.assets.policy, project, self.assets.task_schema)
        return engine.build_result(resolution, self.assets.policy, project, self.assets.execution_plan_schema)

    def test_globs_are_anchored_segment_aware_and_never_match_prose(self):
        cases = [("migrations/**", "migrations", True), ("migrations/**", "migrations/2026/001.sql", True),
                 ("migrations/**", "src/migrations/001.sql", False), ("**/*secret*", "config/app_secret.env", True),
                 ("**/*secret*", "secret", True), ("a/**/b", "a/b", True), ("a/**/b", "a/x/y/b", True),
                 ("a/**/b", "a/xb", False), ("*.sql", "x.sql", True), ("*.sql", "db/x.sql", False),
                 ("src/db?/*.py", "src/db1/m.py", True), ("src/db?/*.py", "src/db10/m.py", False)]
        for pattern, path, expected in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertIs(engine.path_glob_matches(pattern, path), expected)
        self.assertEqual([engine.normalize_repo_path(value) for value in
                          ["./migrations//001.sql/", "/etc/passwd", "../outside", "the CI workflows", 3]],
                         ["migrations/001.sql", None, None, "the CI workflows", None])
        matches = engine.match_path_floors(["the CI workflows", "/abs/migrations/x", "src/app.py"], PROJECT["path_floors"])
        self.assertEqual(matches, [])

    def test_schema_accepts_path_floors_and_rejects_weakening_unknown_and_malformed(self):
        self.assertEqual(engine.validate_project_routing_instance(PROJECT, self.assets.project_schema), [])
        template = engine.load_yaml(ROOT / "templates/project-routing.yaml")
        self.assertEqual(template["path_floors"], [])
        bad_rules = [
            {"paths": ["a/**"], "floors": {"security_sensitive": False}},
            {"paths": ["a/**"], "floors": {"failure_cost": "extreme"}},
            {"paths": ["a/**"], "floors": {"task_type": "migration"}},
            {"paths": ["a/**"], "floors": {}},
            {"paths": [], "floors": {"failure_cost": "high"}},
            {"paths": ["a/**"], "floors": {"failure_cost": "high"}, "route": "CODEX"},
            {"paths": ["a/**", "a/**"], "floors": {"failure_cost": "high"}},
        ]
        bad_rules += [{"paths": [glob], "floors": {"failure_cost": "high"}} for glob in
                      ["/abs/**", "!docs/**", "a//b", "a/", "../x", "a/./b", "src/**.py", "**x", "a/***", "[ab]",
                       "{a,b}/x", "a\\b", "", " a", "a\nb"]]
        for rule in bad_rules:
            with self.subTest(rule=rule):
                self.assertTrue(engine.validate_project_routing_instance({"path_floors": [rule]}, self.assets.project_schema))

    def test_merged_floors_only_raise(self):
        merged = engine.merge_risk_floors([{"failure_cost": "high", "ambiguity": 3}, {"failure_cost": "medium",
                                           "ambiguity": 5, "security_sensitive": True}], self.assets.task_schema)
        self.assertEqual(merged, {"failure_cost": "high", "ambiguity": 5, "security_sensitive": True})
        task = small_task(failure_cost="medium")
        floored = engine.apply_risk_overrides(task, {"failure_cost": "low"}, self.assets.task_schema)
        self.assertEqual(floored["failure_cost"], "medium")

    def test_route_task_applies_path_floors_from_files_or_areas_without_evidence(self):
        unfloored = run_route_task(small_task(), PROJECT, self.dir)
        self.assertEqual((unfloored["ok"], unfloored["selected_route"], unfloored["path_floor_matches"]), (True, "CLAUDE", []))
        # The same small, unflagged task now declares the release workflow: the floor raises security_sensitive.
        task = small_task(files_or_areas=[".github/workflows/release.yml", "README.md"])
        result = run_route_task(task, PROJECT, self.dir)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["selected_route"], "CODEX_CLAUDE_CODEX")
        self.assertFalse(result["classification"]["base"]["security_sensitive"])
        self.assertTrue(result["classification"]["effective"]["security_sensitive"])
        self.assertNotIn("escalation_evidence", task, "a floor-raised value needs no evidence")
        self.assertEqual(result["path_floor_matches"], [{
            "rule": 1, "patterns": [".github/workflows/**"], "floors": {"security_sensitive": True},
            "matched_paths": [".github/workflows/release.yml"], "matched_path_count": 1}])
        self.assertEqual(engine.validate_route_task_result(result, self.assets), [])
        # An escalating value the classifier set itself still needs evidence.
        raw = run_route_task(small_task(security_sensitive=True), PROJECT, self.dir)
        self.assertEqual(raw["blocker_code"], engine.CLASSIFICATION_EVIDENCE_MISSING)

    def test_validator_recomputes_path_floor_matches(self):
        result = self.route(small_task(files_or_areas=["migrations/0042_add.sql"]))
        self.assertEqual(result["selected_route"], "CODEX_CLAUDE_CODEX")
        self.assertEqual(engine.validate_route_task_result(result, self.assets), [])
        for forge in ({"path_floor_matches": []}, {"path_floor_matches": None},
                      {"path_floor_matches": [dict(result["path_floor_matches"][0], matched_path_count=2)]}):
            with self.subTest(forge=forge):
                issues = engine.validate_route_task_result({**result, **forge}, self.assets)
                self.assertIn("result.path_floor_matches", [issue.path for issue in issues])
        # Dropping the floors from project_constraints is caught as well (effective classification, route).
        stripped = copy.deepcopy(result)
        stripped["project_constraints"] = {}
        self.assertTrue(engine.validate_route_task_result(stripped, self.assets))

    def test_diff_touching_a_floored_path_blocks_a_small_unflagged_task(self):
        ran = self.route(small_task())
        self.assertEqual(ran["selected_route"], "CLAUDE")
        verdict = engine.check_diff_path_floors(ran, [".pi/autonomy.yaml", ".github/workflows/release.yml"], self.assets)
        self.assertEqual((verdict["ok"], verdict["blocked"], verdict["blocker_code"]),
                         (True, True, engine.CLASSIFICATION_UNDERRATED_BY_DIFF))
        self.assertTrue(verdict["route_raised"])
        self.assertEqual(verdict["required"]["route"], "CODEX_CLAUDE_CODEX")
        self.assertEqual(verdict["diff_path_floor_matches"][0]["matched_paths"], [".github/workflows/release.yml"])
        # Re-routing with the floored paths declared selects exactly the required route, and its own diff passes.
        rerouted = self.route(small_task(files_or_areas=[".pi/autonomy.yaml", ".github/workflows/release.yml"]))
        self.assertEqual(rerouted["selected_route"], verdict["required"]["route"])
        again = engine.check_diff_path_floors(rerouted, [".pi/autonomy.yaml", ".github/workflows/release.yml"], self.assets)
        self.assertFalse(again["blocked"])

    def test_diff_touching_only_unfloored_paths_changes_nothing(self):
        ran = self.route(small_task())
        for changed in ([], [".pi/autonomy.yaml"], ["src/migrations_helper.py", "docs/schemas.md", "README.md"]):
            with self.subTest(changed=changed):
                verdict = engine.check_diff_path_floors(ran, changed, self.assets)
                self.assertEqual((verdict["ok"], verdict["blocked"], verdict["diff_path_floor_matches"]), (True, False, []))
                self.assertEqual(verdict["required"], verdict["ran"])
        # A floored path that was already declared (and floored) before the run does not block either.
        declared = self.route(small_task(files_or_areas=["schemas/task.schema.json"]))
        self.assertFalse(engine.check_diff_path_floors(declared, ["schemas/task.schema.json"], self.assets)["blocked"])

    def test_capacity_only_raise_blocks_on_the_same_route(self):
        project = {"path_floors": [{"paths": ["config/**"], "floors": {"ambiguity": 4}}]}
        ran = self.route(small_task(), project)
        verdict = engine.check_diff_path_floors(ran, ["config/limits.yaml"], self.assets)
        self.assertTrue(verdict["blocked"])
        self.assertEqual((verdict["route_raised"], verdict["capacity_raised"]), (False, True))
        self.assertEqual(verdict["required"]["route"], "CLAUDE")
        self.assertEqual((verdict["ran"]["capacities"]["claude_worker"], verdict["required"]["capacities"]["claude_worker"]),
                         ("light", "standard"))

    def test_trace_shows_route_path_floor_matches(self):
        result = self.route(small_task(files_or_areas=[f"migrations/{n:03}.sql" for n in range(12)]))
        report = provenance.build_trace_report(self.dir, {"run_id": None, "route_task_result": result, "steps": []})
        self.assertEqual(report["route"]["path_floor_matches"], result["path_floor_matches"])
        text = provenance.render_trace_text(report)
        self.assertIn("  Path floor (files_or_areas): rule 0 ['migrations/**'] floors "
                      "{'failure_cost': 'high', 'behavior_preservation': 'required'} via migrations/000.sql, ", text)
        self.assertIn("migrations/009.sql (+2 more)", text)

    def test_invalid_route_result_is_unverifiable(self):
        ran = self.route(small_task())
        forged = copy.deepcopy(ran)
        forged["selected_route"] = "CODEX_CLAUDE_CODEX"
        for result in (None, {"ok": False}, forged):
            with self.subTest(result=type(result).__name__):
                verdict = engine.check_diff_path_floors(result, [".github/workflows/release.yml"], self.assets)
                self.assertEqual((verdict["ok"], verdict["blocker_code"]), (False, engine.CLASSIFICATION_DIFF_UNVERIFIABLE))


class DiffFloorCheckToolTest(unittest.TestCase):
    """tools/diff_floor_check.py collects the real changed paths of a bound worktree."""

    @classmethod
    def setUpClass(cls):
        cls.assets = engine.load_policy_assets(ROOT)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo = base / "target"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        git("config", "user.email", "test@example.invalid", cwd=self.repo)
        git("config", "user.name", "Test", cwd=self.repo)
        (self.repo / ".pi").mkdir()
        (self.repo / ".pi/autonomy.yaml").write_text("push: false\n")
        (self.repo / "migrations").mkdir()
        (self.repo / "migrations/0001_init.sql").write_text("create table t (id int);\n")
        (self.repo / ".gitignore").write_text("build/\n")
        git("add", ".", cwd=self.repo)
        git("commit", "-q", "-m", "init", cwd=self.repo)
        self.worktree = base / "wt"
        git("worktree", "add", "-q", "-b", "task", str(self.worktree), cwd=self.repo)
        resolution = engine.resolve_route(small_task(), self.assets.policy, PROJECT, self.assets.task_schema)
        self.route = engine.build_result(resolution, self.assets.policy, PROJECT, self.assets.execution_plan_schema)

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, worktree: Path | None = None, route: dict | None = None) -> dict:
        payload = {"route_task_result": route or self.route, "worktree": str(worktree or self.worktree),
                   "parent_checkout": str(self.repo)}
        done = subprocess.run([sys.executable, str(ROOT / "tools/diff_floor_check.py")], input=json.dumps(payload),
                              capture_output=True, text=True, check=True, timeout=60)
        return json.loads(done.stdout)

    def test_unfloored_changes_pass(self):
        (self.worktree / ".pi/autonomy.yaml").write_text("push: true\n")
        (self.worktree / "build").mkdir()
        (self.worktree / "build/migrations.log").write_text("ignored\n")
        verdict = self.check()
        self.assertEqual((verdict["ok"], verdict["blocked"], verdict["changed_path_count"]), (True, False, 1))

    def test_committed_staged_untracked_and_deleted_floored_paths_are_seen(self):
        variants = {
            "committed": lambda wt: ((wt / ".github/workflows").mkdir(parents=True),
                                     (wt / ".github/workflows/ci.yml").write_text("on: push\n"),
                                     git("add", ".", cwd=wt), git("commit", "-q", "-m", "ci", cwd=wt)),
            "untracked": lambda wt: (wt / "config_secret.env").write_text("x=1\n"),
            "deleted": lambda wt: git("rm", "-q", "migrations/0001_init.sql", cwd=wt),
            "renamed": lambda wt: git("mv", "migrations/0001_init.sql", "sql/0001_init.sql", cwd=wt),
        }
        for name, change in variants.items():
            with self.subTest(change=name):
                git("checkout", "-q", "-f", "task", cwd=self.worktree)
                git("reset", "-q", "--hard", "main", cwd=self.worktree)
                git("clean", "-qfd", cwd=self.worktree)
                (self.worktree / "sql").mkdir(exist_ok=True)
                change(self.worktree)
                verdict = self.check()
                self.assertEqual((verdict["ok"], verdict["blocked"], verdict["blocker_code"]),
                                 (True, True, engine.CLASSIFICATION_UNDERRATED_BY_DIFF), verdict)
                self.assertEqual(verdict["base"], git("rev-parse", "main", cwd=self.repo).strip())

    def test_parent_progress_after_the_fork_is_not_the_worker_diff(self):
        # The parent integrated an earlier wave touching a floored path; the worker never did.
        (self.repo / "migrations/0002_more.sql").write_text("alter table t add c int;\n")
        git("add", ".", cwd=self.repo)
        git("commit", "-q", "-m", "earlier wave", cwd=self.repo)
        (self.worktree / ".pi/autonomy.yaml").write_text("push: true\n")
        self.assertFalse(self.check()["blocked"])

    def test_a_worker_cannot_remove_the_floor_in_its_worktree(self):
        (self.worktree / ".pi/routing.yaml").write_text("path_floors: []\n")
        (self.worktree / "migrations/0002_drop.sql").write_text("drop table t;\n")
        self.assertTrue(self.check()["blocked"])

    def test_unbound_or_foreign_paths_are_unverifiable(self):
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        git("init", "-q", cwd=other)
        for path in (self.repo, other, Path(self.tmp.name) / "missing"):
            with self.subTest(path=path.name):
                verdict = self.check(worktree=path)
                self.assertEqual((verdict["ok"], verdict["blocker_code"]), (False, engine.CLASSIFICATION_DIFF_UNVERIFIABLE))
        malformed = subprocess.run([sys.executable, str(ROOT / "tools/diff_floor_check.py")], input="[]",
                                   capture_output=True, text=True, check=True, timeout=60)
        self.assertEqual(json.loads(malformed.stdout)["blocker_code"], engine.CLASSIFICATION_DIFF_UNVERIFIABLE)


class DiffFloorReRouteTest(unittest.TestCase):
    """An underrated diff is a required re-route of the parent run, never a terminal finish."""

    @classmethod
    def setUpClass(cls):
        cls.assets = engine.load_policy_assets(ROOT)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.records = base / "orchestrator"
        (self.records / "schemas").mkdir(parents=True)
        shutil.copy(ROOT / "schemas/run-record.schema.json", self.records / "schemas/run-record.schema.json")
        self.target = base / "target"
        subprocess.run(["git", "init", "-q", str(self.target)], check=True)
        subprocess.run(["git", "-C", str(self.target), "remote", "add", "origin",
                        "git@github.com:Examplecorp/examplelab.git"], check=True)
        self.run_id = po.start(worktree=self.target, repo_root=self.records)["run_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def event(self, data: dict) -> dict:
        return po.record_event(self.run_id, data, repo_root=self.records)

    def route(self, task: dict, project: dict = PROJECT) -> dict:
        resolution = engine.resolve_route(task, self.assets.policy, project, self.assets.task_schema)
        return engine.build_result(resolution, self.assets.policy, project, self.assets.execution_plan_schema)

    def phase(self) -> dict:
        return provenance.get_run(self.records, self.run_id)["parent_orchestration"]["phases"][0]

    def underrated(self, verdict: dict, ran: dict) -> dict:
        """The event the lifecycle extension records for a blocked verdict."""
        return {"kind": "diff_floors", "phase": ran["classification"]["base"]["id"], "status": "underrated",
                "from_route": ran["selected_route"], "to_route": verdict["required"]["route"],
                "capacity_raised": verdict["capacity_raised"], "matches": verdict["diff_path_floor_matches"]}

    def test_underrated_diff_requires_a_reroute_and_resume_reaches_the_higher_route(self):
        ran = self.route(small_task())
        task_id = ran["classification"]["base"]["id"]
        self.event({"kind": "phase", "phase": task_id, "status": "in_progress", "route": ran["selected_route"]})
        self.event({"kind": "worker", "phase": task_id, "step_id": "claude_worker", "status": "in_progress"})
        self.event({"kind": "worker", "phase": task_id, "step_id": "claude_worker", "status": "completed"})
        self.event({"kind": "checkpoint", "phase": task_id, "reason": "worker_approval", "workspace_bound": True})
        changed = [".pi/autonomy.yaml", ".github/workflows/release.yml"]
        verdict = engine.check_diff_path_floors(ran, changed, self.assets)
        self.assertEqual(verdict["blocker_code"], engine.CLASSIFICATION_UNDERRATED_BY_DIFF)
        self.event(self.underrated(verdict, ran))

        phase = self.phase()
        self.assertEqual(phase["status"], "in_progress", "an underrated diff is not a terminal outcome")
        self.assertEqual(phase["escalations"][-1], {**phase["escalations"][-1], "from_route": "CLAUDE",
                         "to_route": "CODEX_CLAUDE_CODEX", "reason": "PATH_FLOOR", "confirmed": False})
        self.assertEqual(phase["diff_floor_checks"][-1]["status"], "underrated")
        self.assertEqual(phase["diff_floor_checks"][-1]["matches"][0]["matched_paths"], [".github/workflows/release.yml"])
        # Resume: the phase is a pending re-route, and nothing completes until it is confirmed.
        self.assertEqual(po.status(self.run_id, repo_root=self.records)["reroute_phases"], [task_id])
        with self.assertRaises(po.ParentRunError) as pending:
            po.finish(self.run_id, "COMPLETED", repo_root=self.records)
        self.assertEqual(pending.exception.error_code, "PARENT_ROUTE_RECHECK_REQUIRED")
        # Re-routing with the old classification cannot confirm it.
        with self.assertRaises(po.ParentRunError) as stale:
            self.event({"kind": "phase", "phase": task_id, "status": "in_progress", "route": "CLAUDE"})
        self.assertEqual(stale.exception.error_code, "PARENT_EVENT_CONFLICT")
        # Re-routing with the floored paths declared reaches the required route and confirms the re-route.
        rerouted = self.route(small_task(files_or_areas=changed))
        self.assertEqual(rerouted["selected_route"], "CODEX_CLAUDE_CODEX")
        self.event({"kind": "phase", "phase": task_id, "status": "in_progress", "route": rerouted["selected_route"]})
        self.assertTrue(self.phase()["escalations"][-1]["confirmed"])
        self.assertFalse(engine.check_diff_path_floors(rerouted, changed, self.assets)["blocked"])
        run = provenance.get_run(self.records, self.run_id)
        text = provenance.render_trace_text(provenance.build_trace_report(self.records, run))
        self.assertIn("Re-route (PATH_FLOOR): CLAUDE -> CODEX_CLAUDE_CODEX; confirmed True", text)
        self.assertIn("Diff floor check (route epoch 0, route CLAUDE): underrated (CLASSIFICATION_UNDERRATED_BY_DIFF) -> CODEX_CLAUDE_CODEX", text)
        self.assertIn("Path floor (diff): rule 1 ['.github/workflows/**'] floors {'security_sensitive': True} via .github/workflows/release.yml", text)

    def test_capacity_only_reroute_keeps_the_route(self):
        project = {"path_floors": [{"paths": ["config/**"], "floors": {"ambiguity": 4}}]}
        ran = self.route(small_task(), project)
        task_id = ran["classification"]["base"]["id"]
        self.event({"kind": "phase", "phase": task_id, "status": "in_progress", "route": "CLAUDE"})
        verdict = engine.check_diff_path_floors(ran, ["config/limits.yaml"], self.assets)
        self.event(self.underrated(verdict, ran))
        self.assertEqual(self.phase()["escalations"][-1]["to_route"], "CLAUDE")
        self.assertEqual(po.status(self.run_id, repo_root=self.records)["reroute_phases"], [task_id])
        self.event({"kind": "phase", "phase": task_id, "status": "in_progress", "route": "CLAUDE"})
        self.assertTrue(self.phase()["escalations"][-1]["confirmed"])

    def test_passed_check_records_matches_without_a_reroute(self):
        declared = self.route(small_task(files_or_areas=["schemas/task.schema.json"]))
        task_id = declared["classification"]["base"]["id"]
        self.event({"kind": "phase", "phase": task_id, "status": "in_progress", "route": declared["selected_route"]})
        verdict = engine.check_diff_path_floors(declared, ["schemas/task.schema.json"], self.assets)
        self.assertFalse(verdict["blocked"])
        self.event({"kind": "diff_floors", "phase": task_id, "status": "passed", "matches": verdict["diff_path_floor_matches"]})
        phase = self.phase()
        self.assertNotIn("escalations", phase)
        self.assertEqual(phase["diff_floor_checks"][0]["status"], "passed")
        self.assertEqual(po.status(self.run_id, repo_root=self.records)["reroute_phases"], [task_id])  # in progress

    def test_invalid_diff_floor_events_change_nothing(self):
        ran = self.route(small_task())
        task_id = ran["classification"]["base"]["id"]
        self.event({"kind": "phase", "phase": task_id, "status": "in_progress", "route": "CLAUDE"})
        verdict = engine.check_diff_path_floors(ran, [".github/workflows/release.yml"], self.assets)
        good = self.underrated(verdict, ran)
        bad_events = [
            {**good, "from_route": "CODEX"},                       # not the current route
            {**good, "to_route": "CLAUDE", "capacity_raised": False},  # same route without a capacity raise
            {**good, "matches": []},                               # no floor matched
            {**good, "status": "lowered"},
            {**good, "matches": [{**good["matches"][0], "matched_paths": ["a\nb"]}]},
            {**good, "matches": [{**good["matches"][0], "floors": {"task_type": "migration"}}]},
            {"kind": "diff_floors", "phase": task_id, "status": "passed", "matches": [], "to_route": "CODEX"},
        ]
        for bad in bad_events:
            with self.subTest(bad=bad):
                with self.assertRaises(po.ParentRunError) as refused:
                    self.event(bad)
                self.assertEqual(refused.exception.error_code, "PARENT_DIFF_FLOORS_INVALID")
        # A lower-precedence target is never a re-route (floors only raise).
        self.event({"kind": "phase", "phase": "other", "status": "in_progress", "route": "CODEX_CLAUDE_CODEX"})
        with self.assertRaises(po.ParentRunError):
            self.event({**good, "phase": "other", "from_route": "CODEX_CLAUDE_CODEX", "to_route": "CLAUDE"})
        self.assertNotIn("escalations", self.phase())
        self.assertNotIn("diff_floor_checks", self.phase())


if __name__ == "__main__":
    unittest.main()
