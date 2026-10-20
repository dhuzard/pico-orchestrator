#!/usr/bin/env python3
"""Deterministic tests for the repository-scoped child launcher on real Git repositories."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import headless_run as hr  # noqa: E402
import provenance  # noqa: E402
import session_scope  # noqa: E402
import workspace_run as wr  # noqa: E402
from test_headless_run import FAKE_PI, git  # noqa: E402


def ok_access(repository: str) -> dict:
    return {"status": "ok", "blocker_code": None, "detail": None, "private": True}


class WorkspaceRunTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.repos = {}
        for name in ("alpha", "beta", "gamma"):
            path = self.base / name
            path.mkdir()
            git("-C", str(path), "init", "-q")
            git("-C", str(path), "remote", "add", "origin", f"git@github.com:acme/{name}.git")
            (path / "README.md").write_text(f"# {name}\n")
            git("-C", str(path), "add", "README.md")
            git("-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init")
            self.repos[name] = (path, git("-C", str(path), "rev-parse", "HEAD"))
        self.state = self.base / "state"
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.pi = self.bin / "pi"
        self.pi.write_text(FAKE_PI)
        self.pi.chmod(0o755)
        self._env = dict(os.environ)
        os.environ[provenance.STATE_ENV] = str(self.state)
        os.environ["FAKE_PI_TOOLS"] = str(REPO_ROOT / "tools")
        os.environ["PI_CODING_AGENT_DIR"] = str(self.base / "agent")
        os.environ["FAKE_PI_BEHAVIOR"] = "finish"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def manifest(self, tasks=None, pins=None) -> Path:
        pins = pins or {}
        data = {"schema_version": 1, "id": "ws", "goal": "Cross-repo goal", "max_parallel": 3,
                "repositories": [{"id": n, "repository": f"acme/{n}", "path": n,
                                  "pinned_commit": pins.get(n, sha)} for n, (_, sha) in self.repos.items()],
                "tasks": tasks or [{"id": "a1", "repository": "alpha", "title": "Alpha adapter"},
                                   {"id": "b1", "repository": "beta", "title": "Beta extraction"},
                                   {"id": "g1", "repository": "gamma", "title": "Gamma identity"}]}
        path = self.base / "workspace.yaml"
        path.write_text(yaml.safe_dump(data))
        return path

    def launch(self, manifest, task, **kwargs):
        return wr.launch(manifest=manifest, task_id=task, access_check=ok_access, pi_executable=str(self.pi), **kwargs)

    def snapshots(self):
        return {n: session_scope.snapshot(str(p))["digest"] for n, (p, _) in self.repos.items()}

    def test_three_children_run_isolated_and_are_linked(self):
        manifest = self.manifest()
        before = self.snapshots()
        results = {t: self.launch(manifest, t) for t in ("a1", "b1", "g1")}
        self.assertEqual(self.snapshots(), before)
        run_ids = set()
        for task, result in results.items():
            entry = result["task"]
            self.assertEqual(entry["run_status"], "PARTIAL", entry)
            self.assertEqual(entry["execution_id"], f"ws--{task}")
            self.assertTrue(entry["run_id"].startswith("run-"))
            run_ids.add(entry["run_id"])
            record = provenance.get_run(REPO_ROOT, entry["run_id"])
            self.assertEqual(record["parent_orchestration"]["target_repository"], entry["target_repository"])
        self.assertEqual(len(run_ids), 3)
        self.assertEqual({r["task"]["target_repository"] for r in results.values()}, {"acme/alpha", "acme/beta", "acme/gamma"})
        workspace = json.loads(Path(results["a1"]["workspace_record"]).read_text())
        self.assertEqual(sorted(workspace["tasks"]), ["a1", "b1", "g1"])
        self.assertEqual(workspace["tasks"]["b1"]["run_id"], results["b1"]["task"]["run_id"])
        self.assertNotIn('"objective":', json.dumps(workspace))
        self.assertNotIn("prompt", json.dumps(workspace))
        # Each execution is bound to its own repository's state key.
        for name, (path, _) in self.repos.items():
            for other in self.repos:
                if other != name:
                    with self.assertRaises(hr.HeadlessError) as ctx:
                        hr.status(target=path, explicit_repo=f"acme/{name}", execution_id=f"ws--{other[0]}1")
                    self.assertEqual(ctx.exception.code, "EXECUTION_UNKNOWN")

    def test_relaunch_is_idempotent_and_crash_resumes_same_run(self):
        manifest = self.manifest()
        first = self.launch(manifest, "a1")
        again = self.launch(manifest, "a1")
        self.assertEqual(again["task"]["outcome"], "already_finished")
        self.assertEqual(again["task"]["launches"], 1)
        self.assertEqual(again["task"]["run_id"], first["task"]["run_id"])
        os.environ["FAKE_PI_BEHAVIOR"] = "crash"
        crashed = self.launch(manifest, "b1")
        self.assertEqual(crashed["task"]["execution_status"], "interrupted")
        os.environ["FAKE_PI_BEHAVIOR"] = "finish"
        blocked = self.launch(manifest, "b1")
        self.assertEqual(blocked["task"]["outcome"], "resume_required")
        resumed = self.launch(manifest, "b1", resume=True)
        self.assertEqual(resumed["task"]["run_id"], crashed["task"]["run_id"])
        self.assertEqual(resumed["task"]["run_status"], "PARTIAL")
        self.assertEqual(resumed["task"]["launches"], 2)
        self.assertEqual(len(list((self.state / "runs").glob("run-*.json"))), 2)

    def test_scoped_objective_is_immutable_and_excludes_workspace_goal(self):
        tasks = [{"id": "a1", "repository": "alpha", "title": "Alpha adapter",
                  "objective": "Implement the alpha adapter using only this checkout."},
                 {"id": "b1", "repository": "beta", "title": "Private beta title"}]
        manifest = self.manifest(tasks)
        result = self.launch(manifest, "a1")
        execution = hr.status(target=self.repos["alpha"][0], explicit_repo="acme/alpha", execution_id="ws--a1")
        self.assertIn(tasks[0]["objective"], execution["objective"])
        self.assertNotIn("Private beta title", execution["objective"])
        self.assertNotIn("Cross-repo goal", execution["objective"])
        self.assertEqual(result["task"]["objective_sha256"], __import__("hashlib").sha256(execution["objective"].encode()).hexdigest())
        tasks[0]["objective"] = "Change the alpha adapter behavior."
        self.manifest(tasks)
        with self.assertRaises(wr.WorkspaceRunError) as rejected:
            self.launch(manifest, "a1")
        self.assertEqual(rejected.exception.code, "EXECUTION_OBJECTIVE_MISMATCH")
        self.assertEqual(self.launch(self.manifest(tasks=[{"id": "new", "repository": "alpha", "title": "Changed",
                                                   "objective": tasks[0]["objective"]}]), "new")["task"]["execution_id"], "ws--new")

    def test_legacy_execution_resumes_with_its_original_objective(self):
        manifest = self.manifest(tasks=[{"id": "a1", "repository": "alpha", "title": "Alpha adapter"}])
        plan = wr._plan(manifest, access_check=ok_access)
        task, repo = wr._task(plan, "a1")
        inputs = wr.dispatch_inputs(plan, task, repo)
        legacy = wr.objective_for(plan, task, repo, inputs).replace(
            "Repository-scoped objective: Alpha adapter\n", "Workspace goal: Cross-repo goal\n", 1)
        os.environ["FAKE_PI_BEHAVIOR"] = "crash"
        hr.start(target=self.repos["alpha"][0], explicit_repo="acme/alpha", execution_id="ws--a1",
                 objective=legacy, pi_executable=str(self.pi))
        os.environ["FAKE_PI_BEHAVIOR"] = "finish"
        resumed = self.launch(manifest, "a1", resume=True)
        self.assertEqual(resumed["task"]["launches"], 2)
        saved = hr.status(target=self.repos["alpha"][0], explicit_repo="acme/alpha", execution_id="ws--a1")
        self.assertEqual(saved["objective"], legacy)

    def test_blocked_task_is_refused_until_upstream_is_ready(self):
        tasks = [{"id": "a1", "repository": "alpha", "title": "Adapter"},
                 {"id": "b1", "repository": "beta", "title": "Consumer", "depends_on": ["a1"]}]
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            self.launch(self.manifest(tasks), "b1")
        self.assertEqual(ctx.exception.code, "WORKSPACE_TASK_BLOCKED")
        self.assertIn("a1", ctx.exception.detail)
        self.assertFalse((self.state / "runs").exists())
        tasks[0]["readiness"] = "implemented"
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            self.launch(self.manifest(tasks), "b1")
        self.assertEqual(ctx.exception.code, "WORKSPACE_TASK_BLOCKED")
        tasks[0]["readiness"] = "ready_for_downstream"
        result = self.launch(self.manifest(tasks), "b1")
        self.assertEqual(result["task"]["run_status"], "PARTIAL")

    def test_pin_mismatch_and_repository_blockers_fail_closed(self):
        path, _ = self.repos["alpha"]
        (path / "extra.txt").write_text("x\n")
        git("-C", str(path), "add", "extra.txt")
        git("-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "more")
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            self.launch(self.manifest(), "a1")
        self.assertEqual(ctx.exception.code, "WORKSPACE_PIN_MISMATCH")
        self.assertFalse((self.state / "runs").exists())
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            self.launch(self.manifest(pins={"beta": "f" * 40}), "b1")
        self.assertEqual(ctx.exception.code, "WORKSPACE_PIN_MISSING")
        git("-C", str(self.repos["gamma"][0]), "remote", "set-url", "origin", "git@github.com:other/gamma.git")
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            self.launch(self.manifest(), "g1")
        self.assertEqual(ctx.exception.code, "TARGET_REPOSITORY_CONFLICT")
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            self.launch(self.manifest(), "zz")
        self.assertEqual(ctx.exception.code, "WORKSPACE_TASK_UNKNOWN")
        self.assertFalse((self.state / "runs").exists())

    def test_cancel_and_status(self):
        manifest = self.manifest()
        os.environ["FAKE_PI_BEHAVIOR"] = "hang"
        launched = self.launch(manifest, "g1", wait=False)
        self.assertEqual(launched["task"]["execution_status"], "launched")
        deadline = time.monotonic() + 5
        while wr.status(manifest=manifest, task_id="g1")["tasks"]["g1"]["run_id"] is None and time.monotonic() < deadline:
            time.sleep(0.05)
        cancelled = wr.cancel(manifest=manifest, task_id="g1")
        self.assertEqual(cancelled["task"]["execution_status"], "cancelled")
        self.assertEqual(cancelled["task"]["run_blocker_code"], "EXECUTION_CANCELLED")
        os.environ["FAKE_PI_BEHAVIOR"] = "finish"
        self.launch(manifest, "a1")
        overall = wr.status(manifest=manifest)
        self.assertEqual(overall["tasks"]["a1"]["run_status"], "PARTIAL")
        self.assertEqual(overall["tasks"]["g1"]["execution_status"], "cancelled")
        self.assertNotIn("b1", overall["tasks"])

    def test_cli(self):
        manifest = self.manifest()
        def run(args):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = wr.main(args)
            return rc, json.loads(out.getvalue())
        rc, data = run(["launch", str(manifest), "--task", "a1", "--no-access-check", "--pi", str(self.pi)])
        self.assertEqual(rc, 0, data)
        self.assertEqual(data["task"]["run_status"], "PARTIAL")
        rc, data = run(["status", str(manifest), "--no-access-check"])
        self.assertEqual(rc, 0)
        self.assertIn("a1", data["tasks"])
        rc, data = run(["launch", str(manifest), "--task", "nope", "--no-access-check", "--pi", str(self.pi)])
        self.assertEqual(rc, 1)
        self.assertEqual(data["error_code"], "WORKSPACE_TASK_UNKNOWN")


if __name__ == "__main__":
    unittest.main()
