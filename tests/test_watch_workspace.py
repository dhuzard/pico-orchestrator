"""The workspace Garden view serves scheduler states, child runs and tokens, never private text."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import watch  # noqa: E402
import workspace_plan  # noqa: E402
import workspace_scheduler  # noqa: E402

SENTINEL = "SENTINEL-PRIVATE-7f3a"
SHA = {"api": "a" * 40, "web": "b" * 40, "docs": "c" * 40}


def usage(lead_in, lead_out, worker_in, worker_out, *, complete=True):
    lead = {"model": "lead-model", "input_tokens": lead_in, "output_tokens": lead_out,
            "total_tokens": lead_in + lead_out, "total_tokens_complete": True}
    worker = {"invocations": 1, "input_tokens": worker_in, "output_tokens": worker_out,
              "total_tokens": worker_in + worker_out if complete else None, "total_tokens_complete": complete,
              "estimated_cost_usd": 0.25}
    return {"lead_usage": lead, "worker_usage": {"claude": worker}, "lead_calls": [
        {"sequence": 1, "model": "lead-model", "input_tokens": lead_in, "output_tokens": lead_out}]}


class WorkspaceViewTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.state = self.base / "state"
        (self.state / "runs").mkdir(parents=True)
        (self.state / "workspaces").mkdir()
        self.manifest = self.base / "workspace.yaml"
        self.manifest.write_text(yaml.safe_dump({
            "schema_version": 1, "id": "garden", "goal": f"Goal {SENTINEL}", "max_parallel": 3,
            # Checkouts are absent: the plan then carries repository blockers but a full task graph.
            "repositories": [{"id": r, "repository": f"acme/{r}", "path": r, "pinned_commit": SHA[r]}
                             for r in ("api", "web", "docs")],
            "tasks": [
                {"id": "api-1", "repository": "api", "title": f"Title {SENTINEL}"},
                {"id": "api-2", "repository": "api", "title": "t"},
                {"id": "web-1", "repository": "web", "title": "t", "depends_on": ["api-1"]},
                {"id": "web-2", "repository": "web", "title": "t", "depends_on": ["api-2"]},
                {"id": "web-3", "repository": "web", "title": "t"},
                {"id": "docs-1", "repository": "docs", "title": "t", "readiness": "merged"},
                {"id": "docs-2", "repository": "docs", "title": "t"},
            ]}))
        self.entries = {
            "api-1": self.entry("api", "exited", "run-api1", "COMPLETED"),
            "api-2": self.entry("api", "exited", "run-api2", "BLOCKED", blocker="WORKER_FAILED"),
            "web-3": self.entry("web", "launched", "run-web3", "IN_PROGRESS",
                                upstream=[{"task": "api-1", "repository": "acme/api",
                                           "pinned_commit": SHA["api"], "readiness": "planned"}]),
        }
        self.write_record()
        self.write_run("run-api1", "acme/api", "COMPLETED", usage(100, 20, 300, 40),
                       phases=[{"id": "build", "status": "completed", "child_run_ids": ["run-pub1"]},
                               {"id": "publication", "status": "completed", "child_run_ids": []}])
        self.write_run("run-api2", "acme/api", "BLOCKED", usage(10, 2, 30, 4), blocker="WORKER_FAILED")
        self.write_run("run-web3", "acme/web", "IN_PROGRESS", usage(50, 5, 7, 1, complete=False))
        (self.state / "runs" / "run-pub1.json").write_text(json.dumps({"run_id": "run-pub1", "github_integration": {
            "target_repository": "acme/api", "audit": [{"operation": "create_draft_pr", "status": "applied", "pr": 12}]}}))
        executions = self.state / "executions" / "repo-key"
        executions.mkdir(parents=True)
        for task in self.entries:
            (executions / f"garden--{task}.json").write_text(json.dumps(
                {"execution_id": f"garden--{task}", "objective": f"Objective {SENTINEL}", "status": "exited"}))
        (executions / "sessions").mkdir()
        (executions / "sessions" / "s.jsonl").write_text(f"transcript {SENTINEL}\n")

    def tearDown(self):
        self._tmp.cleanup()

    def entry(self, repo, status, run_id, run_status, *, blocker=None, upstream=()):
        return {"repository": repo, "target_repository": f"acme/{repo}", "pinned_commit": SHA[repo],
                "execution_id": f"garden--{repo}", "execution_status": status, "outcome": None, "launches": 1,
                "run_id": run_id, "run_status": run_status, "run_blocker_code": blocker,
                "trace_path": f"/home/user/{SENTINEL}/trace.json", "last_seen": "2026-10-20T10:00:00Z",
                "inputs": {"repository": f"acme/{repo}", "pinned_commit": SHA[repo], "upstream": list(upstream),
                           "orchestrator_revision": None}}

    def write_record(self, sessions=None):
        sessions = sessions if sessions is not None else [
            {"id": "2026-10-20T09:00:00Z-11", "started_at": "2026-10-20T09:00:00Z",
             "finished_at": "2026-10-20T09:30:00Z", "cap": 3, "dispatched": ["api-1", "api-2"], "resumed": [],
             "observed_running": [], "refused": {"web-3": "WORKSPACE_PIN_MISMATCH"}, "status": "ATTENTION"},
            {"id": "2026-10-20T10:00:00Z-12", "started_at": "2026-10-20T10:00:00Z", "finished_at": None, "cap": 2,
             "dispatched": ["web-3"], "resumed": [], "observed_running": [],
             "refused": {"docs-2": "WORKSPACE_PIN_MISMATCH"}}]
        record = {"record_version": 1, "workspace_id": "garden", "manifest": str(self.manifest),
                  "manifest_digest": "0" * 64, "orchestrator_revision": None, "created_at": "2026-10-20T09:00:00Z",
                  "updated_at": "2026-10-20T10:00:00Z", "tasks": self.entries, "scheduler_sessions": sessions,
                  "objective": f"Objective {SENTINEL}"}
        (self.state / "workspaces" / "garden.json").write_text(json.dumps(record))

    def write_run(self, run_id, repository, status, context, *, phases=None, blocker=None):
        record = {"run_id": run_id, "started_at": "2026-10-20T09:05:00Z", "outcome": status.lower(),
                  "user_objective": f"Objective {SENTINEL}", "prompt": f"Prompt {SENTINEL}",
                  "raw_output": f"Output {SENTINEL}",
                  "parent_orchestration": {"target_repository": repository, "final_status": status,
                                           "blocker_code": blocker,
                                           "phases": phases or [{"id": "build", "status": "in_progress",
                                                                 "child_run_ids": []}],
                                           "context": {**context, "private_prompt": SENTINEL},
                                           "transcript": SENTINEL}}
        (self.state / "runs" / f"{run_id}.json").write_text(json.dumps(record))

    def view(self, **kwargs):
        return watch.workspace_view(self.state, **kwargs)

    def tasks(self, view):
        return {task["id"]: task for repo in view["repositories"] for task in repo["tasks"]}

    def test_groups_tasks_by_repository_in_manifest_order(self):
        view = self.view(manifest=self.manifest)
        self.assertEqual(view["workspace_id"], "garden")
        self.assertEqual(view["source"], "manifest")
        self.assertEqual([r["id"] for r in view["repositories"]], ["api", "web", "docs"])
        self.assertEqual([r["repository"] for r in view["repositories"]], ["acme/api", "acme/web", "acme/docs"])
        groups = {r["id"]: [t["id"] for t in r["tasks"]] for r in view["repositories"]}
        self.assertEqual(groups, {"api": ["api-1", "api-2"], "web": ["web-3", "web-1", "web-2"],
                                  "docs": ["docs-1", "docs-2"]})
        tasks = self.tasks(view)
        self.assertEqual(tasks["web-1"]["wave"], 1)
        self.assertEqual(tasks["web-1"]["depends_on"], ["api-1"])
        self.assertEqual(tasks["api-1"]["blocks"], ["web-1"])
        self.assertEqual(tasks["web-1"]["blocked_by"], ["api-1"])
        self.assertEqual(tasks["docs-1"]["readiness"], "merged")
        self.assertEqual(view["waves"], [["api-1", "api-2", "docs-1", "docs-2", "web-3"], ["web-1", "web-2"]])

    def test_states_match_the_scheduler_report(self):
        view = self.view(manifest=self.manifest)
        plan = workspace_plan.plan(self.manifest, access_check=None)
        expected = workspace_scheduler.report(plan, self.entries, {"docs-2": {"code": "WORKSPACE_PIN_MISMATCH"}})
        tasks = self.tasks(view)
        self.assertEqual({row["id"]: row["state"] for row in expected["tasks"]},
                         {task_id: task["state"] for task_id, task in tasks.items()})
        self.assertEqual(view["counts"], expected["counts"])
        self.assertEqual(view["status"], expected["status"])
        self.assertEqual(tasks["api-1"]["state"], "completed")
        self.assertEqual(tasks["api-2"]["state"], "failed")
        self.assertEqual(tasks["api-2"]["run_blocker_code"], "WORKER_FAILED")
        self.assertEqual(tasks["web-2"]["state"], "blocked_by_failure")
        self.assertEqual(tasks["web-1"]["state"], "waiting_for_readiness")
        self.assertEqual(tasks["web-3"]["state"], "running")
        self.assertEqual(tasks["docs-1"]["state"], "declared_merged")
        # Only the latest session's refusals apply, and only to tasks not launched since.
        self.assertEqual(tasks["docs-2"]["state"], "refused")
        self.assertEqual(tasks["docs-2"]["refusal_code"], "WORKSPACE_PIN_MISMATCH")
        self.assertEqual(tasks["web-3"]["refusal_code"], None)

    def test_child_runs_links_and_sessions(self):
        tasks = self.tasks(self.view(manifest=self.manifest))
        self.assertEqual(tasks["api-1"]["run"]["id"], "run-api1")
        self.assertEqual(tasks["api-1"]["run"]["status"], "COMPLETED")
        self.assertEqual(tasks["api-1"]["run"]["phases_completed"], 2)
        self.assertEqual(tasks["api-1"]["links"], [{"kind": "pr", "number": 12,
                                                    "url": "https://github.com/acme/api/pull/12"}])
        self.assertEqual(tasks["web-3"]["links"], [])
        self.assertIsNone(tasks["web-1"]["run"])
        sessions = self.view(manifest=self.manifest)["sessions"]
        self.assertEqual([s["id"] for s in sessions], ["2026-10-20T10:00:00Z-12", "2026-10-20T09:00:00Z-11"])
        self.assertEqual(sessions[0]["cap"], 2)
        self.assertIsNone(sessions[0]["finished_at"])
        self.assertEqual(sessions[1]["dispatched"], ["api-1", "api-2"])
        self.assertEqual(sessions[1]["refused"], {"web-3": "WORKSPACE_PIN_MISMATCH"})
        self.assertEqual(sessions[1]["status"], "ATTENTION")

    def test_run_from_another_repository_is_not_attributed(self):
        self.write_run("run-api1", "acme/other", "COMPLETED", usage(1, 1, 1, 1))
        tasks = self.tasks(self.view(manifest=self.manifest))
        self.assertIsNone(tasks["api-1"]["run"])
        self.assertIsNone(tasks["api-1"]["tokens"])
        self.assertEqual(tasks["api-1"]["state"], "completed")  # the state is the scheduler's, from the record

    def test_token_totals_add_up(self):
        view = self.view(manifest=self.manifest)
        tasks = self.tasks(view)
        self.assertEqual(tasks["api-1"]["tokens"], {"input_tokens": 400, "output_tokens": 60, "cached_tokens": None,
                                                     "total_tokens": 460, "estimated_cost_usd": 0.25})
        self.assertIsNone(tasks["web-3"]["tokens"]["total_tokens"])  # a worker total was not reported
        repos = {r["id"]: r["tokens"] for r in view["repositories"]}
        self.assertEqual(repos["api"]["input_tokens"], 400 + 40)
        self.assertEqual(repos["api"]["output_tokens"], 60 + 6)
        self.assertEqual(repos["api"]["total_tokens"], 460 + 46)
        self.assertTrue(repos["api"]["total_complete"])
        self.assertEqual(repos["api"]["runs"], 2)
        self.assertEqual(repos["web"]["input_tokens"], 57)
        self.assertFalse(repos["web"]["total_complete"])
        self.assertEqual(repos["docs"], {"runs": 0, "input_tokens": None, "output_tokens": None, "cached_tokens": None,
                                         "total_tokens": None, "estimated_cost_usd": None, "total_complete": False})
        total = view["tokens"]
        for field in ("input_tokens", "output_tokens"):
            self.assertEqual(total[field], sum(r[field] or 0 for r in repos.values()))
        self.assertEqual(total["total_tokens"], 506)
        self.assertFalse(total["total_complete"])
        self.assertEqual(total["runs"], 3)
        self.assertEqual(total["estimated_cost_usd"], 0.75)

    def test_observed_contract_state_shows_and_unblocks_like_the_scheduler(self):
        self.entries["api-1"]["observed"] = {"readiness": "ready_for_downstream", "validated_commit": SHA["api"],
                                             "validated_contracts": {}}
        self.write_record()
        tasks = self.tasks(self.view(manifest=self.manifest))
        self.assertEqual(tasks["api-1"]["state"], "ready_for_downstream")
        self.assertEqual(tasks["api-1"]["readiness"], "ready_for_downstream")
        # The promoted upstream no longer blocks web-1, exactly as in the scheduler's plan.
        self.assertEqual(tasks["web-1"]["blocked_by"], [])
        self.assertNotIn(SENTINEL, json.dumps(self.view(manifest=self.manifest)))

    def test_open_scheduler_session_shows_dispatched_tasks_as_running(self):
        self.write_record(sessions=[{"id": "2026-10-20T11:00:00Z-12", "started_at": "2026-10-20T11:00:00Z",
                                     "finished_at": None, "cap": 3, "dispatched": ["web-2", "docs-2"],
                                     "resumed": [], "qualified": [], "observed_running": [], "refused": {},
                                     "status": None}])
        view = self.view(manifest=self.manifest)
        tasks = self.tasks(view)
        self.assertEqual((tasks["web-2"]["state"], tasks["docs-2"]["state"]), ("running", "running"))
        self.assertEqual(tasks["api-1"]["state"], "completed")
        self.assertEqual(view["counts"].get("running"), 3)  # web-3 was already launched

    def test_workspace_published_pr_is_linked_only_when_its_publication_run_confirms_it(self):
        (self.state / "runs" / "run-wspub.json").write_text(json.dumps({"run_id": "run-wspub", "github_integration": {
            "target_repository": "acme/web", "audit": [{"operation": "create_draft_pr", "status": "applied", "pr": 7}]}}))
        self.entries["web-3"]["observed"] = {"pr": {"number": 7, "repository": "acme/web", "publication_run_id": "run-wspub"}}
        self.entries["api-2"]["observed"] = {"pr": {"number": 99, "repository": "acme/api", "publication_run_id": "run-wspub"}}
        self.write_record()
        tasks = self.tasks(self.view(manifest=self.manifest))
        self.assertEqual([l["url"] for l in tasks["web-3"]["links"]], ["https://github.com/acme/web/pull/7"])
        self.assertEqual(tasks["api-2"]["links"], [])  # the publication run is for another repository

    def test_record_only_mode_uses_the_workspace_record(self):
        view = self.view(workspace_id="garden")
        self.assertEqual(view["source"], "record")
        self.assertEqual([r["id"] for r in view["repositories"]], ["api", "web"])
        self.assertEqual(view["repositories"][0]["pinned_commit"], SHA["api"])
        tasks = self.tasks(view)
        self.assertEqual(set(tasks), {"api-1", "api-2", "web-3"})
        self.assertEqual(tasks["web-3"]["depends_on"], ["api-1"])
        self.assertEqual(tasks["api-1"]["blocks"], ["web-3"])
        self.assertIsNone(tasks["web-3"]["readiness"])
        self.assertEqual({t: tasks[t]["state"] for t in tasks},
                         {t: workspace_scheduler.classify(self.entries[t]) for t in self.entries})

    def test_unknown_or_invalid_workspace_is_a_clear_error(self):
        with self.assertRaises(watch.WorkspaceViewError) as unknown:
            self.view(workspace_id="nope")
        self.assertEqual((unknown.exception.code, unknown.exception.http_status), ("WORKSPACE_UNKNOWN", 404))
        with self.assertRaises(watch.WorkspaceViewError) as invalid:
            self.view(workspace_id="../runs/run-api1")
        self.assertEqual(invalid.exception.code, "WORKSPACE_ID_INVALID")
        broken = self.base / "broken.yaml"
        broken.write_text(yaml.safe_dump({"schema_version": 1, "id": "x", "goal": SENTINEL}))
        with self.assertRaises(watch.WorkspaceViewError) as bad:
            self.view(manifest=broken)
        self.assertEqual(bad.exception.code, "WORKSPACE_MANIFEST_INVALID")
        self.assertNotIn(SENTINEL, bad.exception.detail)

    def test_manifest_without_record_shows_the_plan(self):
        (self.state / "workspaces" / "garden.json").unlink()
        view = self.view(manifest=self.manifest)
        self.assertFalse(view["record_found"])
        self.assertEqual(self.tasks(view)["api-1"]["state"], "not_launched")
        self.assertEqual(self.tasks(view)["web-1"]["state"], "waiting_for_readiness")

    def serve(self, workspace):
        server = ThreadingHTTPServer(("127.0.0.1", 0), watch.make_handler(self.state / "runs", None, workspace))
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def get(self, url):
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_http_endpoints_never_serve_private_text(self):
        for target in (self.manifest, "garden"):
            base = self.serve(watch.WorkspaceSource(self.state, target))
            status, body = self.get(f"{base}/api/workspace")
            self.assertEqual(status, 200)
            data = json.loads(body)
            self.assertTrue(data["ok"])
            self.assertEqual(data["workspace_id"], "garden")
            status, runs = self.get(f"{base}/api/runs")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(runs)["workspace"], "garden")
            self.assertEqual({run["id"] for run in json.loads(runs)["runs"]}, {"run-api1", "run-api2", "run-web3"})
            for text in (body, runs):
                self.assertNotIn(SENTINEL, text)
                for word in ("objective", "prompt", "transcript", "trace_path", "title", "goal", "raw_output"):
                    self.assertNotIn(f'"{word}"', text)

    def test_http_unknown_workspace_and_mode_off(self):
        base = self.serve(watch.WorkspaceSource(self.state, "missing"))
        status, body = self.get(f"{base}/api/workspace")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error_code"], "WORKSPACE_UNKNOWN")
        self.assertEqual(json.loads(self.get(f"{base}/api/runs")[1])["runs"], [])
        base = self.serve(None)
        status, body = self.get(f"{base}/api/workspace")
        self.assertEqual((status, json.loads(body)["error_code"]), (404, "WORKSPACE_MODE_OFF"))


if __name__ == "__main__":
    unittest.main()
