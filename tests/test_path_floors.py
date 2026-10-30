#!/usr/bin/env python3
"""Path-triggered risk floors (#76): built-in floors and `.pi/routing.yaml`
`path_floors` applied by route_task from a task's files_or_areas, recomputed by
the route-result validator, checked against a worker's real diff before and
after integration, and re-checked by workspace validation and publication.

Deterministic: no model, no network, no GitHub. Git runs only on temporary
repositories."""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import parent_orchestration as po  # noqa: E402
import provenance  # noqa: E402
import routing_engine as engine  # noqa: E402

PROJECT = {
    "path_floors": [
        {"paths": ["migrations/**", "schemas/**"], "floors": {"failure_cost": "high", "behavior_preservation": "required"}},
        {"paths": ["deploy/**", "**/*secret*"], "floors": {"security_sensitive": True}},
    ],
}
CAPACITY_ONLY = {"path_floors": [{"paths": ["tuning/**"], "floors": {"ambiguity": 4}}]}


def small_task(**changes) -> dict:
    """A small, unflagged config edit on an unfloored path: routed CLAUDE with a light worker."""
    case = next(c for c in engine.load_yaml(ROOT / "tests/routing-cases.yaml")["cases"] if c["id"] == "case-17-config-edit")
    task = copy.deepcopy(case["task"])
    task.update({"files_or_areas": ["src/settings.py"], **changes})
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


class Routed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets = engine.load_policy_assets(ROOT)

    def route(self, task: dict, project: dict | None = PROJECT) -> dict:
        resolution = engine.resolve_route(task, self.assets.policy, project, self.assets.task_schema)
        return engine.build_result(resolution, self.assets.policy, project, self.assets.execution_plan_schema)


class PathFloorRoutingTest(Routed):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_globs_are_anchored_segment_aware_and_never_match_prose(self):
        cases = [("migrations/**", "migrations", True), ("migrations/**", "migrations/2026/001.sql", True),
                 ("migrations/**", "src/migrations/001.sql", False), ("**/*secret*", "config/app_secret.env", True),
                 ("**/*secret*", "secret", True), ("a/**/b", "a/b", True), ("a/**/b", "a/x/y/b", True),
                 ("a/**/b", "a/xb", False), ("*.sql", "x.sql", True), ("*.sql", "db/x.sql", False),
                 ("src/db?/*.py", "src/db1/m.py", True), ("src/db?/*.py", "src/db10/m.py", False),
                 # Case-sensitive, documented.
                 ("migrations/**", "Migrations/x.sql", False)]
        for pattern, path, expected in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertIs(engine.path_glob_matches(pattern, path), expected)
        self.assertEqual([engine.normalize_repo_path(value) for value in
                          ["./migrations//001.sql/", "/etc/passwd", "../outside", "the CI workflows", 3,
                           "migrations/a\tb.sql", "migrations\\x", "a/../b"]],
                         ["migrations/001.sql", None, None, "the CI workflows", None, None, None, None])
        matches = engine.match_path_floors(["the CI workflows", "/abs/migrations/x", "src/app.py"], PROJECT["path_floors"])
        self.assertEqual(matches, [])

    def test_unicode_spellings_are_compared_in_nfc(self):
        nfc, nfd = unicodedata.normalize("NFC", "données"), unicodedata.normalize("NFD", "données")
        self.assertNotEqual(nfc, nfd)
        for pattern, path in ((f"{nfc}/**", f"{nfd}/x"), (f"{nfd}/**", f"{nfc}/x")):
            self.assertTrue(engine.path_glob_matches(pattern, path))
        self.assertEqual(engine.normalize_repo_path(f"{nfd}/x"), f"{nfc}/x")

    def test_declared_ancestor_directory_touches_deeper_floors_but_root_never_does(self):
        rules = [{"paths": ["src/db/migrations/**"], "floors": {"failure_cost": "high"}},
                 {"paths": ["**/*secret*"], "floors": {"security_sensitive": True}}]
        for area, expected in (("src", [0]), ("src/db", [0]), ("src/db/migrations", [0]), ("src/db/migrations/1.sql", [0]),
                               ("src/api", []), (".", []), ("/", []), ("./", []), ("srcx", [])):
            with self.subTest(area=area):
                self.assertEqual([m["rule"] for m in engine.match_path_floors([area], rules, declared=True)], expected)
        # Real changed paths are matched as paths only: a changed file named `src` is not an ancestor.
        self.assertEqual(engine.match_path_floors(["src"], rules), [])
        routed = self.route(small_task(files_or_areas=["src"]),
                            {"path_floors": [{"paths": ["src/db/migrations/**"], "floors": {"failure_cost": "high"}}]})
        self.assertEqual(routed["selected_route"], "CODEX_CLAUDE_CODEX")
        self.assertEqual(routed["path_floor_matches"][0]["matched_paths"], ["src"])

    def test_builtin_floors_cover_agent_configuration_and_workflows(self):
        for path in (".pi/autonomy.yaml", ".pi/routing.yaml", ".claude/settings.json", ".claude/hooks/pre.sh",
                     ".mcp.json", ".github/workflows/ci.yml"):
            with self.subTest(path=path):
                routed = self.route(small_task(files_or_areas=[path]), None)
                self.assertEqual(routed["selected_route"], "CODEX_CLAUDE_CODEX")
                self.assertTrue(routed["classification"]["effective"]["security_sensitive"])
                self.assertEqual(routed["path_floor_matches"][0]["source"], "builtin")
                # And a diff touching it after an unfloored run is underrated, with no project config at all.
                ran = self.route(small_task(), None)
                verdict = engine.check_diff_path_floors(ran, ["src/settings.py", path], self.assets)
                self.assertEqual(verdict["blocker_code"], engine.CLASSIFICATION_UNDERRATED_BY_DIFF)
        for path in (".github/dependabot.yml", "pi/x", "docs/.claude.md", ".mcp.json.bak"):
            with self.subTest(unfloored=path):
                self.assertEqual(engine.match_all_path_floors([path], None, self.assets.policy), [])

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
        task = small_task(files_or_areas=["deploy/release.sh", "README.md"])
        result = run_route_task(task, PROJECT, self.dir)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["selected_route"], "CODEX_CLAUDE_CODEX")
        self.assertFalse(result["classification"]["base"]["security_sensitive"])
        self.assertTrue(result["classification"]["effective"]["security_sensitive"])
        self.assertNotIn("escalation_evidence", task, "a floor-raised value needs no evidence")
        self.assertEqual(result["path_floor_matches"], [{
            "source": "project", "rule": 1, "patterns": ["deploy/**"], "floors": {"security_sensitive": True},
            "matched_paths": ["deploy/release.sh"], "matched_path_count": 1}])
        self.assertEqual(engine.validate_route_task_result(result, self.assets), [])
        raw = run_route_task(small_task(security_sensitive=True), PROJECT, self.dir)
        self.assertEqual(raw["blocker_code"], engine.CLASSIFICATION_EVIDENCE_MISSING)

    def test_validator_recomputes_path_floor_matches(self):
        result = self.route(small_task(files_or_areas=["migrations/0042_add.sql"]))
        self.assertEqual(result["selected_route"], "CODEX_CLAUDE_CODEX")
        self.assertEqual(engine.validate_route_task_result(result, self.assets), [])
        for forge in ({"path_floor_matches": []}, {"path_floor_matches": None},
                      {"path_floor_matches": [dict(result["path_floor_matches"][0], matched_path_count=2)]},
                      {"path_floor_matches": [dict(result["path_floor_matches"][0], source="builtin")]}):
            with self.subTest(forge=forge):
                issues = engine.validate_route_task_result({**result, **forge}, self.assets)
                self.assertIn("result.path_floor_matches", [issue.path for issue in issues])
        stripped = copy.deepcopy(result)
        stripped["project_constraints"] = {}
        self.assertTrue(engine.validate_route_task_result(stripped, self.assets))

    def test_all_project_rules_and_builtin_fit_in_run_record(self):
        project = {"path_floors": [{"paths": [".pi/**"], "floors": {"security_sensitive": True}}
                                   for _ in range(64)]}
        self.assertFalse(engine.validate_project_routing_instance(project, self.assets.project_schema))
        matches = engine.match_all_path_floors([".pi/routing.yaml"], project)
        self.assertEqual(len(matches), 65)
        self.assertEqual(len(po._path_floor_matches(matches)), 65)
        schema = json.loads((ROOT / "schemas/run-record.schema.json").read_text())
        phase = schema["properties"]["parent_orchestration"]["properties"]["phases"]["items"]
        self.assertEqual(phase["properties"]["routing"]["properties"]["path_floor_matches"]["maxItems"], 65)
        self.assertEqual(phase["properties"]["diff_floor_checks"]["items"]["properties"]["matches"]["maxItems"], 65)

    def test_recorded_paths_are_bounded(self):
        long_path = "migrations/" + "x" * 5000 + ".sql"
        match = engine.match_path_floors([long_path], PROJECT["path_floors"])[0]
        self.assertLessEqual(len(match["matched_paths"][0]), engine.RECORDED_PATH_MAX)
        self.assertTrue(match["matched_paths"][0].endswith("chars)"))
        self.assertEqual(match["matched_path_count"], 1)

    def test_diff_touching_a_floored_path_blocks_a_small_unflagged_task(self):
        ran = self.route(small_task())
        self.assertEqual(ran["selected_route"], "CLAUDE")
        verdict = engine.check_diff_path_floors(ran, ["src/settings.py", "deploy/release.sh"], self.assets)
        self.assertEqual((verdict["ok"], verdict["blocked"], verdict["blocker_code"]),
                         (True, True, engine.CLASSIFICATION_UNDERRATED_BY_DIFF))
        self.assertTrue(verdict["route_raised"])
        self.assertEqual(verdict["required"]["route"], "CODEX_CLAUDE_CODEX")
        self.assertEqual(verdict["diff_path_floor_matches"][0]["matched_paths"], ["deploy/release.sh"])
        rerouted = self.route(small_task(files_or_areas=["src/settings.py", "deploy/release.sh"]))
        self.assertEqual(rerouted["selected_route"], verdict["required"]["route"])
        self.assertFalse(engine.check_diff_path_floors(rerouted, ["deploy/release.sh"], self.assets)["blocked"])

    def test_diff_touching_only_unfloored_paths_changes_nothing(self):
        ran = self.route(small_task())
        for changed in ([], ["src/settings.py"], ["src/migrations_helper.py", "docs/schemas.md", "README.md"]):
            with self.subTest(changed=changed):
                verdict = engine.check_diff_path_floors(ran, changed, self.assets)
                self.assertEqual((verdict["ok"], verdict["blocked"], verdict["diff_path_floor_matches"]), (True, False, []))
                self.assertEqual(verdict["required"], verdict["ran"])
        declared = self.route(small_task(files_or_areas=["schemas/task.schema.json"]))
        self.assertFalse(engine.check_diff_path_floors(declared, ["schemas/task.schema.json"], self.assets)["blocked"])

    def test_capacity_only_raise_blocks_on_the_same_route(self):
        ran = self.route(small_task(), CAPACITY_ONLY)
        verdict = engine.check_diff_path_floors(ran, ["tuning/limits.yaml"], self.assets)
        self.assertTrue(verdict["blocked"])
        self.assertEqual((verdict["route_raised"], verdict["capacity_raised"]), (False, True))
        self.assertEqual(verdict["required"]["route"], "CLAUDE")
        self.assertEqual((verdict["ran"]["capacities"]["claude_worker"], verdict["required"]["capacities"]["claude_worker"]),
                         ("light", "standard"))

    def test_unusable_changed_paths_are_unverifiable_never_dropped(self):
        ran = self.route(small_task())
        for path in ("migrations/a\tb.sql", "src/a\x01secret", "migrations\\x.sql", "/abs/x", "../x"):
            with self.subTest(path=path):
                verdict = engine.check_diff_path_floors(ran, ["src/settings.py", path], self.assets)
                self.assertEqual((verdict["ok"], verdict["blocker_code"]), (False, engine.CLASSIFICATION_DIFF_UNVERIFIABLE))

    def test_invalid_route_result_is_unverifiable(self):
        ran = self.route(small_task())
        forged = copy.deepcopy(ran)
        forged["selected_route"] = "CODEX_CLAUDE_CODEX"
        for result in (None, {"ok": False}, forged):
            with self.subTest(result=type(result).__name__):
                verdict = engine.check_diff_path_floors(result, ["deploy/x"], self.assets)
                self.assertEqual((verdict["ok"], verdict["blocker_code"]), (False, engine.CLASSIFICATION_DIFF_UNVERIFIABLE))

    def test_trace_shows_route_path_floor_matches(self):
        result = self.route(small_task(files_or_areas=[f"migrations/{n:03}.sql" for n in range(12)]))
        report = provenance.build_trace_report(self.dir, {"run_id": None, "route_task_result": result, "steps": []})
        self.assertEqual(report["route"]["path_floor_matches"], result["path_floor_matches"])
        text = provenance.render_trace_text(report)
        self.assertIn("  Path floor (files_or_areas): project rule 0 ['migrations/**'] floors "
                      "{'failure_cost': 'high', 'behavior_preservation': 'required'} via migrations/000.sql, ", text)
        self.assertIn("migrations/009.sql (+2 more)", text)

    def test_raw_control_characters_fail_closed_before_normalization(self):
        ran = self.route(small_task())
        for control in ("\t", "\r", "\n", "\x00", "\x7f"):
            for path in (control + "src/file.py", "src/file.py" + control):
                with self.subTest(path=repr(path)):
                    self.assertIsNone(engine.normalize_repo_path(path))
                    self.assertEqual(engine.check_diff_path_floors(ran, [path], self.assets)["blocker_code"],
                                     engine.CLASSIFICATION_DIFF_UNVERIFIABLE)

    def test_raw_filename_whitespace_is_preserved_for_matching(self):
        project = {"path_floors": [{"paths": ["secret?"], "floors": {"security_sensitive": True}}]}
        self.assertEqual(engine.normalize_repo_path("secret "), "secret ")
        verdict = engine.check_diff_path_floors(self.route(small_task(), project), ["secret "], self.assets)
        self.assertTrue(verdict["blocked"])

    def test_builtin_floors_are_loaded_from_policy_and_validated(self):
        policy = copy.deepcopy(self.assets.policy)
        policy["path_floors"] = [{"paths": ["custom/**"], "floors": {"failure_cost": "high"}}]
        resolved = engine.resolve_route(small_task(files_or_areas=["custom/x"]), policy, None,
                                        self.assets.task_schema)
        self.assertEqual(resolved.effective_classification["failure_cost"], "high")
        self.assertEqual(engine.match_all_path_floors([".pi/x"], None, policy), [])
        for invalid in (None, [{"paths": ["../escape"], "floors": {"security_sensitive": True}}],
                        [{"paths": ["custom/**"], "floors": {"security_sensitive": False}}]):
            policy["path_floors"] = invalid
            issues = []
            engine.validate_builtin_path_floors(policy, self.assets.project_schema, issues)
            self.assertTrue(issues)
        del policy["path_floors"]
        issues = []
        engine.validate_builtin_path_floors(policy, self.assets.project_schema, issues)
        self.assertTrue(issues)

    def test_recorded_routing_check_for_workspace_validation(self):
        schema = self.assets.task_schema
        low = {"failure_cost": "low", "semantic_correctness": "normal", "ambiguity": 2, "diagnosis_required": False,
               "implementation_scope": "small", "signals": []}
        high = {**low, "failure_cost": "high", "signals": ["behavior_preservation_required"]}
        verdict = engine.check_recorded_path_floors(["src/x.py"], PROJECT, [], schema, self.assets.policy)
        self.assertEqual((verdict["ok"], verdict["blocked"]), (True, False))
        verdict = engine.check_recorded_path_floors(["migrations/1.sql"], PROJECT, [low], schema, self.assets.policy)
        self.assertEqual(verdict["blocker_code"], engine.CLASSIFICATION_UNDERRATED_BY_DIFF)
        self.assertFalse(engine.check_recorded_path_floors(["migrations/1.sql"], PROJECT, [low, high], schema, self.assets.policy)["blocked"])
        self.assertEqual(engine.check_recorded_path_floors(["migrations/1.sql"], PROJECT, [], schema, self.assets.policy)["blocker_code"],
                         engine.CLASSIFICATION_DIFF_UNVERIFIABLE)
        self.assertEqual(engine.check_recorded_path_floors([".pi/autonomy.yaml"], None, [high], schema, self.assets.policy)["blocker_code"],
                         engine.CLASSIFICATION_UNDERRATED_BY_DIFF)
        self.assertFalse(engine.check_recorded_path_floors([".pi/autonomy.yaml"], None,
                                                           [{**low, "signals": ["security_sensitive"]}], schema, self.assets.policy)["blocked"])
        self.assertEqual(engine.check_recorded_path_floors(["a\tb"], None, [high], schema, self.assets.policy)["blocker_code"],
                         engine.CLASSIFICATION_DIFF_UNVERIFIABLE)


class DiffFloorCheckToolTest(Routed):
    """tools/diff_floor_check.py collects the real changed paths of a bound worktree or of the merged change."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo = base / "target"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        git("config", "user.email", "test@example.invalid", cwd=self.repo)
        git("config", "user.name", "Test", cwd=self.repo)
        (self.repo / "src").mkdir()
        (self.repo / "src/settings.py").write_text("PUSH = False\n")
        (self.repo / "migrations").mkdir()
        (self.repo / "migrations/0001_init.sql").write_text("create table t (id int);\n")
        (self.repo / ".gitignore").write_text("build/\n")
        git("add", ".", cwd=self.repo)
        git("commit", "-q", "-m", "init", cwd=self.repo)
        self.worktree = base / "wt"
        git("worktree", "add", "-q", "-b", "task", str(self.worktree), cwd=self.repo)
        self.ran = self.route(small_task())

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, payload: dict) -> dict:
        done = subprocess.run([sys.executable, str(ROOT / "tools/diff_floor_check.py")], input=json.dumps(payload),
                              capture_output=True, text=True, check=True, timeout=60)
        return json.loads(done.stdout)

    def check(self, worktree: Path | None = None, **extra) -> dict:
        return self.run_tool({"route_task_result": self.ran, "worktree": str(worktree or self.worktree),
                              "parent_checkout": str(self.repo), **extra})

    def reset(self):
        git("checkout", "-q", "-f", "task", cwd=self.worktree)
        git("reset", "-q", "--hard", "main", cwd=self.worktree)
        git("clean", "-qfd", cwd=self.worktree)

    def test_unfloored_changes_pass(self):
        (self.worktree / "src/settings.py").write_text("PUSH = True\n")
        (self.worktree / "build").mkdir()
        (self.worktree / "build/migrations.log").write_text("ignored\n")
        verdict = self.check()
        self.assertEqual((verdict["ok"], verdict["blocked"], verdict["changed_path_count"]), (True, False, 1))
        self.assertEqual(verdict["changed_paths"], ["src/settings.py"])
        self.assertEqual(verdict["head"], git("rev-parse", "HEAD", cwd=self.worktree).strip())

    def test_committed_staged_untracked_and_deleted_floored_paths_are_seen(self):
        variants = {
            "committed": lambda wt: ((wt / "deploy").mkdir(parents=True),
                                     (wt / "deploy/run.sh").write_text("echo\n"),
                                     git("add", ".", cwd=wt), git("commit", "-q", "-m", "ci", cwd=wt)),
            "untracked": lambda wt: (wt / "config_secret.env").write_text("x=1\n"),
            "deleted": lambda wt: git("rm", "-q", "migrations/0001_init.sql", cwd=wt),
            "renamed": lambda wt: git("mv", "migrations/0001_init.sql", "sql/0001_init.sql", cwd=wt),
            "builtin": lambda wt: ((wt / ".claude").mkdir(), (wt / ".claude/settings.json").write_text("{}\n")),
        }
        for name, change in variants.items():
            with self.subTest(change=name):
                self.reset()
                (self.worktree / "sql").mkdir(exist_ok=True)
                change(self.worktree)
                verdict = self.check()
                self.assertEqual((verdict["ok"], verdict["blocked"], verdict["blocker_code"]),
                                 (True, True, engine.CLASSIFICATION_UNDERRATED_BY_DIFF), verdict)
                self.assertEqual(verdict["base"], git("rev-parse", "main", cwd=self.repo).strip())

    def test_control_characters_and_hidden_edits_are_unverifiable(self):
        variants = {
            "control-char untracked": lambda wt: (wt / "migrations/a\tb.sql").write_text("drop\n"),
            "control-char committed": lambda wt: ((wt / "migrations/a\tb.sql").write_text("drop\n"),
                                                  git("add", "-A", cwd=wt), git("commit", "-qm", "c", cwd=wt)),
            "assume-unchanged": lambda wt: (git("update-index", "--assume-unchanged", "migrations/0001_init.sql", cwd=wt),
                                            (wt / "migrations/0001_init.sql").write_text("changed\n")),
            "skip-worktree": lambda wt: (git("update-index", "--skip-worktree", "migrations/0001_init.sql", cwd=wt),
                                         (wt / "migrations/0001_init.sql").write_text("changed\n")),
        }
        for name, change in variants.items():
            with self.subTest(change=name):
                git("update-index", "--no-assume-unchanged", "--no-skip-worktree", "migrations/0001_init.sql", cwd=self.worktree)
                self.reset()
                change(self.worktree)
                verdict = self.check()
                self.assertEqual((verdict["ok"], verdict["blocker_code"]), (False, engine.CLASSIFICATION_DIFF_UNVERIFIABLE), verdict)

    def test_parent_progress_after_the_fork_is_not_the_worker_diff(self):
        (self.repo / "migrations/0002_more.sql").write_text("alter table t add c int;\n")
        git("add", ".", cwd=self.repo)
        git("commit", "-q", "-m", "earlier wave", cwd=self.repo)
        (self.worktree / "src/settings.py").write_text("PUSH = True\n")
        self.assertFalse(self.check()["blocked"])

    def test_a_worker_cannot_remove_the_floor_in_its_worktree(self):
        (self.worktree / "migrations/0002_drop.sql").write_text("drop table t;\n")
        (self.worktree / "routing.yaml").write_text("path_floors: []\n")
        self.assertTrue(self.check()["blocked"])

    def test_missing_earlier_worktree_is_reported_only_when_allowed(self):
        gone = Path(self.tmp.name) / "removed"
        self.assertEqual(self.check(worktree=gone, allow_missing=True)["missing"], True)
        self.assertEqual(self.check(worktree=gone)["blocker_code"], engine.CLASSIFICATION_DIFF_UNVERIFIABLE)

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

    def test_parent_evidence_binds_clean_head_and_full_attributed_paths(self):
        base = git("rev-parse", "HEAD", cwd=self.repo).strip()
        (self.repo / "src/settings.py").write_text("PUSH = True\n")
        payload = {"operation": "parent", "parent_checkout": str(self.repo), "base": base,
                   "members": [{"task_id": "one", "route_task_result": self.ran, "exclude_paths": []}]}
        dirty = self.run_tool(payload)
        self.assertNotIn("head", dirty)
        self.assertNotIn("path_digests", dirty["verdicts"][0])
        git("add", "-A", cwd=self.repo)
        git("commit", "-qm", "change", cwd=self.repo)
        clean = self.run_tool(payload)
        self.assertEqual(clean["head"], git("rev-parse", "HEAD", cwd=self.repo).strip())
        self.assertEqual(clean["verdicts"][0]["path_digests"],
                         [hashlib.sha256(b"src/settings.py").hexdigest()])
        payload["members"][0]["exclude_paths"] = ["src/settings.py"]
        self.assertEqual(self.run_tool(payload)["verdicts"][0]["path_digests"], [])

    def test_merged_change_is_checked_at_integrate_end(self):
        base = git("rev-parse", "HEAD", cwd=self.repo).strip()
        payload = lambda members: {"operation": "parent", "parent_checkout": str(self.repo), "base": base,  # noqa: E731
                                   "members": members}
        member = {"task_id": self.ran["classification"]["base"]["id"], "route_task_result": self.ran, "exclude_paths": []}
        (self.repo / "src/settings.py").write_text("PUSH = True\n")
        self.assertEqual(self.run_tool(payload([member]))["blocked"], False)
        # A branch merged from elsewhere (the worktree's HEAD stayed clean) still lands in the merged change.
        git("checkout", "-qb", "decoy", cwd=self.worktree)
        (self.worktree / "migrations/0002.sql").write_text("y\n")
        git("add", "-A", cwd=self.worktree)
        git("commit", "-qm", "decoy", cwd=self.worktree)
        git("merge", "-q", "--no-edit", "decoy", cwd=self.repo)
        verdict = self.run_tool(payload([member]))
        self.assertEqual((verdict["ok"], verdict["blocked"], verdict["blocker_code"]),
                         (True, True, engine.CLASSIFICATION_UNDERRATED_BY_DIFF))
        self.assertEqual(verdict["verdicts"][0]["diff_path_floor_matches"][0]["matched_paths"], ["migrations/0002.sql"])
        # A path another member's checked worktree changed is that member's, not this one's.
        self.assertFalse(self.run_tool(payload([{**member, "exclude_paths": ["migrations/0002.sql"]}]))["blocked"])
        self.assertEqual(self.run_tool({**payload([member]), "base": "0" * 40})["blocker_code"],
                         engine.CLASSIFICATION_DIFF_UNVERIFIABLE)


class DiffFloorReRouteTest(Routed):
    """An underrated diff is a required re-route of the parent run, never a terminal finish."""

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

    def phase(self, index: int = 0) -> dict:
        return provenance.get_run(self.records, self.run_id)["parent_orchestration"]["phases"][index]

    def routed_phase(self, result: dict) -> str:
        task_id = result["classification"]["base"]["id"]
        self.event({"kind": "phase", "phase": task_id, "status": "in_progress", "route": result["selected_route"],
                    "route_task_result": result})
        return task_id

    def underrated(self, verdict: dict, ran: dict) -> dict:
        """The event the lifecycle extension records for a blocked verdict."""
        paths = sorted({p for match in verdict["diff_path_floor_matches"] for p in match["matched_paths"]})
        return {"kind": "diff_floors", "phase": ran["classification"]["base"]["id"], "status": "underrated",
                "from_route": ran["selected_route"], "to_route": verdict["required"]["route"],
                "capacity_raised": verdict["capacity_raised"], "matches": verdict["diff_path_floor_matches"],
                "route_task_result": ran, "paths": paths, "head": "c" * 40}

    def test_parent_diff_evidence_requires_integrating_wave_and_is_schema_valid(self):
        task = self.routed_phase(self.route(small_task()))
        evidence = {"kind": "diff_floors", "phase": task, "status": "passed", "matches": [],
                    "operation": "parent", "wave_id": "w1", "base": "a" * 40, "head": "b" * 40,
                    "path_digests": [hashlib.sha256(b"src/settings.py").hexdigest()]}
        with self.assertRaises(po.ParentRunError):
            self.event(evidence)
        self.event({"kind": "wave", "wave_id": "w1", "task_ids": [task], "status": "declared"})
        self.event({"kind": "wave", "wave_id": "w1", "task_ids": [task], "status": "integrating"})
        self.event(evidence)
        check = self.phase()["diff_floor_checks"][-1]
        self.assertEqual((check["operation"], check["base"], check["head"], check["wave_id"], check["route_epoch"]),
                         ("parent", "a" * 40, "b" * 40, "w1", 0))
        self.assertEqual(check["path_digests"], evidence["path_digests"])
        self.event({"kind": "wave", "wave_id": "w1", "task_ids": [task], "status": "abandoned"})

    def test_underrated_diff_requires_a_reroute_and_resume_reaches_the_higher_route(self):
        ran = self.route(small_task())
        task_id = self.routed_phase(ran)
        self.event({"kind": "worker", "phase": task_id, "step_id": "claude_worker", "status": "in_progress"})
        self.event({"kind": "worker", "phase": task_id, "step_id": "claude_worker", "status": "completed"})
        self.event({"kind": "wave", "wave_id": "w1", "task_ids": [task_id], "status": "declared"})
        self.event({"kind": "checkpoint", "phase": task_id, "reason": "worker_approval", "workspace_bound": True})
        changed = ["src/settings.py", "deploy/release.sh"]
        verdict = engine.check_diff_path_floors(ran, changed, self.assets)
        self.assertEqual(verdict["blocker_code"], engine.CLASSIFICATION_UNDERRATED_BY_DIFF)
        self.event(self.underrated(verdict, ran))

        phase = self.phase()
        self.assertEqual(phase["status"], "in_progress", "an underrated diff is not a terminal outcome")
        escalation = phase["escalations"][-1]
        self.assertEqual({k: escalation[k] for k in ("from_route", "to_route", "reason", "confirmed", "paths")},
                         {"from_route": "CLAUDE", "to_route": "CODEX_CLAUDE_CODEX", "reason": "PATH_FLOOR",
                          "confirmed": False, "paths": ["deploy/release.sh"]})
        self.assertEqual(escalation["required_capacities"]["claude_worker"], "high")
        self.assertEqual(phase["diff_floor_checks"][-1]["head"], "c" * 40)
        status = po.status(self.run_id, repo_root=self.records)
        self.assertEqual((status["reroute_phases"], status["bound_phases"]), ([task_id], [task_id]))
        with self.assertRaises(po.ParentRunError) as pending:
            po.finish(self.run_id, "COMPLETED", repo_root=self.records)
        self.assertEqual(pending.exception.error_code, "PARENT_ROUTE_RECHECK_REQUIRED")
        # The wave cannot integrate while the re-route is unconfirmed (probe_parent: it used to be accepted).
        for wave_status in ("integrating", "integrated"):
            with self.assertRaises(po.ParentRunError) as refused:
                self.event({"kind": "wave", "wave_id": "w1", "task_ids": [task_id], "status": wave_status})
            self.assertEqual(refused.exception.error_code, "PARENT_ROUTE_RECHECK_REQUIRED")
        with self.assertRaises(po.ParentRunError) as stale:
            self.event({"kind": "phase", "phase": task_id, "status": "in_progress", "route": "CLAUDE",
                        "route_task_result": ran})
        self.assertEqual(stale.exception.error_code, "PARENT_EVENT_CONFLICT")
        # The required route without the route result, or with one that does not cover the paths, is refused.
        rerouted = self.route(small_task(files_or_areas=changed))
        self.assertEqual(rerouted["selected_route"], "CODEX_CLAUDE_CODEX")
        with self.assertRaises(po.ParentRunError) as bare:
            self.event({"kind": "phase", "phase": task_id, "status": "in_progress", "route": "CODEX_CLAUDE_CODEX"})
        self.assertEqual(bare.exception.error_code, engine.CLASSIFICATION_UNDERRATED_BY_DIFF)
        with self.assertRaises(po.ParentRunError) as foreign:
            self.event({"kind": "phase", "phase": task_id, "status": "in_progress", "route": "CODEX_CLAUDE_CODEX",
                        "route_task_result": self.route(small_task(id="other-task", files_or_areas=changed))})
        self.assertEqual(foreign.exception.error_code, "PARENT_DIFF_FLOORS_INVALID")
        self.assertFalse(self.phase()["escalations"][-1]["confirmed"])
        self.event({"kind": "phase", "phase": task_id, "status": "in_progress", "route": rerouted["selected_route"],
                    "route_task_result": rerouted})
        self.assertTrue(self.phase()["escalations"][-1]["confirmed"])
        self.event({"kind": "wave", "wave_id": "w1", "task_ids": [task_id], "status": "integrating"})
        run = provenance.get_run(self.records, self.run_id)
        text = provenance.render_trace_text(provenance.build_trace_report(self.records, run))
        self.assertIn("Re-route (PATH_FLOOR): CLAUDE -> CODEX_CLAUDE_CODEX; confirmed True", text)
        self.assertIn("Diff floor check (route epoch 0, route CLAUDE): underrated (CLASSIFICATION_UNDERRATED_BY_DIFF) -> CODEX_CLAUDE_CODEX", text)
        self.assertIn("Path floor (diff): project rule 1 ['deploy/**'] floors {'security_sensitive': True} via deploy/release.sh", text)

    def test_capacity_only_reroute_needs_a_result_that_covers_the_paths(self):
        ran = self.route(small_task(), CAPACITY_ONLY)
        task_id = self.routed_phase(ran)
        verdict = engine.check_diff_path_floors(ran, ["tuning/limits.yaml"], self.assets)
        self.event(self.underrated(verdict, ran))
        self.assertEqual(self.phase()["escalations"][-1]["to_route"], "CLAUDE")
        self.assertEqual(po.status(self.run_id, repo_root=self.records)["reroute_phases"], [task_id])
        # The same classification rerun (same route, same light worker) no longer confirms it.
        with self.assertRaises(po.ParentRunError) as same:
            self.event({"kind": "phase", "phase": task_id, "status": "in_progress", "route": "CLAUDE",
                        "route_task_result": self.route(small_task(), CAPACITY_ONLY)})
        self.assertEqual(same.exception.error_code, engine.CLASSIFICATION_UNDERRATED_BY_DIFF)
        self.assertFalse(self.phase()["escalations"][-1]["confirmed"])
        covering = self.route(small_task(files_or_areas=["src/settings.py", "tuning/limits.yaml"]), CAPACITY_ONLY)
        self.assertEqual(covering["capacity_assignments"][0]["assigned_capacity_tier"], "standard")
        self.event({"kind": "phase", "phase": task_id, "status": "in_progress", "route": "CLAUDE",
                    "route_task_result": covering})
        self.assertTrue(self.phase()["escalations"][-1]["confirmed"])

    def test_the_parent_reruns_the_engine_and_accepts_only_the_required_route(self):
        ran = self.route(small_task())
        task_id = self.routed_phase(ran)
        verdict = engine.check_diff_path_floors(ran, ["deploy/x.sh"], self.assets)
        good = self.underrated(verdict, ran)
        bad_events = [
            {**good, "from_route": "CODEX"},                            # not the current route
            {**good, "to_route": "CODEX"},                              # an earlier route, but not the required one
            {**good, "to_route": "CLAUDE", "capacity_raised": True},    # same route
            {**good, "paths": ["src/settings.py"]},                     # paths that the engine does not floor
            {**good, "paths": []},
            {**good, "paths": ["deploy/../x"]},
            {**{k: v for k, v in good.items() if k != "route_task_result"}},
            {**good, "route_task_result": self.route(small_task(id="other-task"))},
            {**good, "matches": []},
            {**good, "status": "lowered"},
            {**good, "head": "not-a-commit"},
            {**good, "matches": [{**good["matches"][0], "matched_paths": ["a\nb"]}]},
            {**good, "matches": [{**good["matches"][0], "source": "plugin"}]},
            {**good, "matches": [{**good["matches"][0], "floors": {"task_type": "migration"}}]},
            {"kind": "diff_floors", "phase": task_id, "status": "passed", "matches": [], "to_route": "CODEX"},
        ]
        for bad in bad_events:
            with self.subTest(bad={k: v for k, v in bad.items() if k != "route_task_result"}):
                with self.assertRaises(po.ParentRunError) as refused:
                    self.event(bad)
                self.assertEqual(refused.exception.error_code, "PARENT_DIFF_FLOORS_INVALID")
        self.assertNotIn("escalations", self.phase())
        self.assertNotIn("diff_floor_checks", self.phase())
        self.event(good)
        self.assertEqual(self.phase()["escalations"][-1]["to_route"], "CODEX_CLAUDE_CODEX")

    def fill_escalations(self, route: str) -> None:
        """Record MAX_ESCALATIONS_PER_PHASE confirmed escalations on the first phase, staying on `route`."""
        record = provenance.get_run(self.records, self.run_id)
        phase = record["parent_orchestration"]["phases"][0]
        phase["escalations"] = [{"from_route": route, "to_route": route, "reason": "AMBIGUITY", "at": record["started_at"],
                                 "confirmed": True} for _ in range(po.MAX_ESCALATIONS_PER_PHASE)]
        provenance.update_parent_orchestration(self.records, self.run_id, record["parent_orchestration"])

    def test_lead_escalations_are_capped_and_the_run_stays_resumable(self):
        ran = self.route(small_task())
        task_id = self.routed_phase(ran)
        self.event({"kind": "escalation", "phase": task_id, "from_route": "CLAUDE", "to_route": "CLAUDE_CODEX",
                    "reason": "AMBIGUITY"})
        self.fill_escalations("CLAUDE")
        with self.assertRaises(po.ParentRunError) as capped:
            self.event({"kind": "escalation", "phase": task_id, "from_route": "CLAUDE", "to_route": "CLAUDE_CODEX",
                        "reason": "AMBIGUITY"})
        self.assertEqual(capped.exception.error_code, "PARENT_ESCALATION_LIMIT")
        self.assertEqual(len(self.phase()["escalations"]), po.MAX_ESCALATIONS_PER_PHASE)
        self.assertEqual(provenance.get_run(self.records, self.run_id)["outcome"], "in_progress")

    def test_capped_underrated_diff_is_refused_with_the_limit_code(self):
        ran = self.route(small_task(), CAPACITY_ONLY)
        task_id = self.routed_phase(ran)
        verdict = engine.check_diff_path_floors(ran, ["tuning/limits.yaml"], self.assets)
        self.fill_escalations("CLAUDE")
        with self.assertRaises(po.ParentRunError) as capped:
            self.event(self.underrated(verdict, ran))
        self.assertEqual(capped.exception.error_code, "PARENT_ESCALATION_LIMIT")
        self.assertEqual(po.status(self.run_id, repo_root=self.records)["reroute_phases"], [task_id])
        po.finish(self.run_id, "PARTIAL", repo_root=self.records)

    def test_passed_check_records_matches_and_routing_summary_paths_are_masked(self):
        declared = self.route(small_task(files_or_areas=["schemas/task.schema.json"]))
        task_id = declared["classification"]["base"]["id"]
        token = "ghp_" + "a" * 30
        match = dict(declared["path_floor_matches"][0], matched_paths=[f"schemas/{token}.json"])
        self.event({"kind": "phase", "phase": task_id, "status": "in_progress", "route": declared["selected_route"],
                    "routing": {"task_type": "config_edit", "ambiguity": 2, "failure_cost": "high",
                                "semantic_correctness": "normal", "diagnosis_required": False,
                                "implementation_scope": "small", "signals": [], "matches": [], "fallback_applied": False,
                                "assignments": [], "path_floor_matches": [match]}})
        self.assertNotIn(token, json.dumps(self.phase()["routing"]))
        verdict = engine.check_diff_path_floors(declared, ["schemas/task.schema.json"], self.assets)
        self.assertFalse(verdict["blocked"])
        self.event({"kind": "diff_floors", "phase": task_id, "status": "passed", "matches": verdict["diff_path_floor_matches"]})
        phase = self.phase()
        self.assertNotIn("escalations", phase)
        self.assertEqual(phase["diff_floor_checks"][0]["status"], "passed")


class WorkspaceValidationFloorsTest(unittest.TestCase):
    """`pico workspace validate` and `publish` re-check the work commit against the pinned commit's floors."""

    def setUp(self):
        import workspace_contracts as wc
        self.wc = wc
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "alpha"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        git("config", "user.email", "test@example.invalid", cwd=self.repo)
        git("config", "user.name", "Test", cwd=self.repo)
        (self.repo / ".pi").mkdir()
        (self.repo / ".pi/routing.yaml").write_text(json.dumps(PROJECT))
        (self.repo / "README.md").write_text("x\n")
        git("add", ".", cwd=self.repo)
        git("commit", "-q", "-m", "init", cwd=self.repo)
        self.pin = git("rev-parse", "HEAD", cwd=self.repo).strip()
        self.plan = {"workspace_id": "ws"}
        self.task = {"id": "a1"}
        self.repo_entry = {"resolved_path": str(self.repo), "pinned_commit": self.pin}
        self.record = {"parent_orchestration": {"phases": [], "waves": []}}
        self._patches = [unittest.mock.patch.object(wc, "_entry", return_value={"run_id": "r"}),
                         unittest.mock.patch.object(wc, "_recorded_run", side_effect=lambda run_id: self.record)]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        self.tmp.cleanup()

    def commit(self, path: str, content: str = "x\n") -> str:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", path, cwd=self.repo)
        return git("rev-parse", "HEAD", cwd=self.repo).strip()

    def check(self, commit: str) -> dict:
        return self.wc.path_floor_check(self.plan, self.task, self.repo_entry, commit)

    def test_unfloored_commit_passes_without_a_run(self):
        self.assertEqual(self.check(self.commit("src/a.py"))["blocked"], False)

    def add_evidence(self, commit: str, paths: list[str], *, failure_cost: str = "high",
                     signals: list[str] | None = None) -> dict:
        summary = {"failure_cost": failure_cost, "semantic_correctness": "normal", "ambiguity": 2,
                   "diagnosis_required": False, "implementation_scope": "small",
                   "signals": signals if signals is not None else ["behavior_preservation_required"],
                   "assignments": [{"step_id": "implement"}, {"step_id": "review"}]}
        phase_id = f"phase-{len(self.record['parent_orchestration']['phases'])}"
        wave_id = f"wave-{phase_id}"
        phase = {"id": phase_id, "status": "completed", "route": "CODEX_CLAUDE_CODEX", "routing": summary,
                 "workers": [{"step_id": step, "status": "completed", "route_epoch": 0}
                             for step in ("implement", "review")],
                 "diff_floor_checks": [{"status": "passed", "operation": "parent", "base": self.pin,
                                        "head": commit, "wave_id": wave_id, "route_epoch": 0,
                                        "route": "CODEX_CLAUDE_CODEX", "matches": [],
                                        "path_digests": sorted(hashlib.sha256(path.encode()).hexdigest()
                                                               for path in paths)}]}
        self.record["parent_orchestration"]["phases"].append(phase)
        self.record["parent_orchestration"]["waves"].append({"id": wave_id, "status": "integrated", "task_ids": [phase_id]})
        return phase

    def test_floored_commit_needs_completed_commit_bound_evidence(self):
        commit = self.commit("migrations/1.sql")
        self.assertEqual(self.check(commit)["blocker_code"], engine.CLASSIFICATION_DIFF_UNVERIFIABLE)
        phase = self.add_evidence(commit, ["migrations/1.sql"], failure_cost="low", signals=[])
        self.assertEqual(self.check(commit)["blocker_code"], engine.CLASSIFICATION_UNDERRATED_BY_DIFF)
        with self.assertRaises(self.wc.WorkspaceRunError):
            self.wc._refuse_underrated(self.check(commit))
        phase["routing"].update(failure_cost="high", signals=["behavior_preservation_required"])
        self.assertFalse(self.check(commit)["blocked"])

    def test_unrelated_summary_and_old_worktree_checks_do_not_authorize_publish(self):
        commit = self.commit(".pi/autonomy.yaml")
        phase = self.add_evidence(commit, ["src/unrelated.py"], signals=["security_sensitive"])
        self.assertEqual(self.check(commit)["blocker_code"], engine.CLASSIFICATION_DIFF_UNVERIFIABLE)
        phase["diff_floor_checks"][0]["path_digests"] = [hashlib.sha256(b".pi/autonomy.yaml").hexdigest()]
        phase["diff_floor_checks"][0].pop("operation")
        self.assertEqual(self.check(commit)["blocker_code"], engine.CLASSIFICATION_DIFF_UNVERIFIABLE)

    def test_incomplete_or_stale_epoch_and_wave_evidence_fails_closed(self):
        commit = self.commit("migrations/1.sql")
        self.add_evidence(commit, ["migrations/1.sql"])
        original = copy.deepcopy(self.record)
        mutations = [
            lambda p: p.update(status="in_progress"),
            lambda p: p["workers"][1].update(status="not_started"),
            lambda p: p["workers"][1].update(route_epoch=1),
            lambda p: p["diff_floor_checks"][0].update(route_epoch=1),
            lambda p: p.update(escalations=[{"confirmed": False}]),
            lambda p: p.update(escalations=[{"confirmed": True, "routing": p["routing"], "to_route": p["route"]}]),
            lambda p: p["diff_floor_checks"][0].update(wave_id="unrelated"),
            lambda p: self.record["parent_orchestration"]["waves"][0].update(status="integrating"),
        ]
        for mutate in mutations:
            self.record = copy.deepcopy(original)
            mutate(self.record["parent_orchestration"]["phases"][0])
            self.assertEqual(self.check(commit)["blocker_code"], engine.CLASSIFICATION_DIFF_UNVERIFIABLE)

    def test_integrated_content_remains_valid_across_unrelated_later_commits(self):
        first = self.commit("migrations/1.sql")
        self.add_evidence(first, ["migrations/1.sql"])
        later = self.commit("src/other.py")
        self.assertFalse(self.check(later)["blocked"])
        changed = self.commit("migrations/1.sql", "changed after review\n")
        self.assertEqual(self.check(changed)["blocker_code"], engine.CLASSIFICATION_DIFF_UNVERIFIABLE)

    def test_evidence_must_include_real_changed_path_in_checked_commit(self):
        first = self.commit("src/other.py")
        later = self.commit("migrations/1.sql")
        self.add_evidence(first, ["migrations/1.sql"])
        self.assertEqual(self.check(later)["blocker_code"], engine.CLASSIFICATION_DIFF_UNVERIFIABLE)

    def test_separate_phases_can_cover_separate_floored_paths(self):
        first = self.commit("migrations/1.sql")
        self.add_evidence(first, ["migrations/1.sql"])
        second = self.commit(".pi/autonomy.yaml")
        self.add_evidence(second, [".pi/autonomy.yaml"], failure_cost="low", signals=["security_sensitive"])
        self.assertFalse(self.check(second)["blocked"])

    def test_literal_git_pathspec_and_raw_control_characters(self):
        commit = self.commit(":(glob)file")
        self.add_evidence(commit, [":(glob)file"])
        evidence = self.wc._recorded_path_routing("r", self.repo, self.pin, commit, [":(glob)file"])
        self.assertEqual(len(evidence[":(glob)file"]), 1)
        commit = self.commit("src/file.py\n")
        self.assertEqual(self.check(commit)["blocker_code"], engine.CLASSIFICATION_DIFF_UNVERIFIABLE)

    def test_the_work_cannot_remove_the_pinned_floors(self):
        self.commit(".pi/routing.yaml", "path_floors: []\n")
        commit = self.commit("migrations/1.sql")
        self.add_evidence(commit, [".pi/routing.yaml", "migrations/1.sql"], failure_cost="low", signals=["security_sensitive"])
        self.assertEqual(self.check(commit)["blocker_code"], engine.CLASSIFICATION_UNDERRATED_BY_DIFF)


if __name__ == "__main__":
    unittest.main()
