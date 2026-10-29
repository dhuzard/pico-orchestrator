#!/usr/bin/env python3
"""Mutation authorization of workspace tasks: preflight, awaiting state, approval and recovery.

Reproduces the scientific-review-four-repo-proposal-v1 failure with the fake `pi`:
two upstream tasks in repositories without an autonomy policy stopped BLOCKED
asking for human mutation approval (no worker, no mutation), and their five
downstream tasks were reported as blocked by a failure.
"""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import garden_controls as gc  # noqa: E402
import headless_run as hr  # noqa: E402
import usage  # noqa: E402
import watch  # noqa: E402
import workspace_authorization as wa  # noqa: E402
import workspace_run as wr  # noqa: E402
import workspace_scheduler as ws  # noqa: E402
from test_headless_run import git, stage_mutation_policy  # noqa: E402
from test_workspace_scheduler import SchedulerFixture  # noqa: E402

COMMIT = ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m"]
UPSTREAM = ("mm-schema-sync", "dp-handoff-controls")
DOWNSTREAM = ("crt-sep-import", "crt-occurrence-export", "oratlas-mm-consumer", "oratlas-crt-consumer", "qualify")


class AuthorizationFixture(SchedulerFixture):
    def setUp(self):
        super().setUp()
        os.environ["FAKE_PI_BEHAVIOR"] = "finish"
        for name in self.repos:
            git("-C", str(self.repos[name][0]), "branch", "-M", "main")

    def repin(self, name: str, message: str) -> str:
        path, _ = self.repos[name]
        git("-C", str(path), *COMMIT, message)
        self.repos[name] = (path, git("-C", str(path), "rev-parse", "HEAD"))
        return self.repos[name][1]

    def remove_policy(self, name: str) -> None:
        git("-C", str(self.repos[name][0]), "rm", "-q", ".pi/autonomy.yaml")
        self.repin(name, "no autonomy policy")

    def set_policy(self, name: str, **grants) -> None:
        stage_mutation_policy(self.repos[name][0], **grants)
        self.repin(name, "policy")

    def scientific_review(self) -> Path:
        """The observed workspace's shape: alpha=murmuration, beta=diggypaper, gamma=crt, delta=oratlas."""
        tasks = [{"id": "mm-schema-sync", "repository": "alpha", "title": "Sync exporter schema"},
                 {"id": "dp-handoff-controls", "repository": "beta", "title": "Freeze packet contract"},
                 {"id": "crt-sep-import", "repository": "gamma", "title": "SEP importer", "depends_on": ["dp-handoff-controls"]},
                 {"id": "crt-occurrence-export", "repository": "gamma", "title": "Occurrence export",
                  "depends_on": ["crt-sep-import"]},
                 {"id": "oratlas-mm-consumer", "repository": "delta", "title": "Murmuration consumer",
                  "depends_on": ["mm-schema-sync"]},
                 {"id": "oratlas-crt-consumer", "repository": "delta", "title": "CRT consumer",
                  "depends_on": ["crt-occurrence-export"]},
                 {"id": "qualify", "repository": "delta", "title": "Replay locks", "kind": "qualification",
                  "depends_on": ["oratlas-mm-consumer", "oratlas-crt-consumer"],
                  "checks": [{"id": "locks", "repository": "delta", "argv": [sys.executable, "-c", "pass"]}]},
                 {"id": "crt-docs", "repository": "gamma", "title": "Independent docs"}]
        return self.manifest(tasks, max_parallel=1)

    def launched(self) -> bool:
        return self.log.exists() and bool(self.log.read_text().strip())

    def execution(self, task_id: str, attempt: int = 1) -> Path:
        name = {"mm-schema-sync": "alpha", "dp-handoff-controls": "beta"}.get(task_id, "gamma")
        common = Path(git("-C", str(self.repos[name][0]), "rev-parse", "--absolute-git-dir"))
        return hr.execution_path(common, wr.execution_id("ws", task_id, attempt))

    def legacy_block(self, manifest: Path, task_id: str) -> None:
        """Launch as the runtime did before this check existed: no preflight, the lead blocks on its own."""
        plan = wr._plan(manifest, access_check=None)
        task, repo = wr._task(plan, task_id)
        os.environ["FAKE_PI_BEHAVIOR"] = "blocked"
        os.environ["FAKE_PI_BLOCKER"] = "HUMAN_MUTATION_APPROVAL_REQUIRED"
        hr.start(target=Path(repo["resolved_path"]), explicit_repo=repo["repository"],
                 execution_id=wr.execution_id("ws", task_id), objective=wr.objective_for(plan, task, repo),
                 allow_mutation=True, pi_executable=str(self.pi))
        os.environ["FAKE_PI_BEHAVIOR"] = "finish"
        wr.status(manifest=manifest)

    def states(self, result) -> dict:
        return {row["id"]: row["state"] for row in result["tasks"]}


class PreflightTest(AuthorizationFixture):
    def test_missing_policies_launch_nothing_and_dependents_wait_for_authorization(self):
        for name in ("alpha", "beta", "delta"):
            self.remove_policy(name)
        manifest = self.scientific_review()
        result = self.schedule(manifest)
        states = self.states(result)
        self.assertEqual({t: states[t] for t in UPSTREAM}, dict.fromkeys(UPSTREAM, "awaiting_authorization"))
        self.assertEqual({t: states[t] for t in DOWNSTREAM}, dict.fromkeys(DOWNSTREAM, "waiting_for_authorization"))
        self.assertEqual(states["crt-docs"], "partial")  # independent work still runs
        self.assertEqual(result["status"], "AWAITING_AUTHORIZATION")
        self.assertEqual(result["session"]["dispatched"], ["crt-docs"])
        self.assertEqual(result["session"]["awaiting_authorization"], sorted(UPSTREAM))
        for task_id in UPSTREAM:  # no execution, no run, no tokens
            self.assertFalse(self.execution(task_id).exists())
        self.assertEqual(self.log.read_text().count("start"), 1)
        requests = {r["repository"]: r for r in result["approval_requests"]}
        self.assertEqual(sorted(requests), ["alpha", "beta"])
        self.assertEqual((requests["beta"]["code"], requests["beta"]["missing"], requests["beta"]["policy_source"],
                          requests["beta"]["tasks"]),
                         ("MUTATION_AUTHORIZATION_REQUIRED", ["create_worktrees", "commit"], "default", ["dp-handoff-controls"]))
        text = ws.render(result, manifest)
        self.assertIn("Awaiting human authorization", text)
        self.assertIn("beta (acme/beta): MUTATION_AUTHORIZATION_REQUIRED for dp-handoff-controls", text)
        self.assertIn("no .pi/autonomy.yaml is committed at the pinned commit", text)
        self.assertIn("This grants no push, pull request, mark-ready, merge or deployment permission.", text)
        # delta has no policy either: its tasks are flagged now, so every approval can be given in one pass.
        later = result["later_approval_requests"]
        self.assertEqual([(r["repository"], r["tasks"]) for r in later],
                         [("delta", ["oratlas-crt-consumer", "oratlas-mm-consumer"])])
        self.assertIn("Later tasks will also need approval in delta (acme/delta)", text)
        self.assertNotIn("blocked_by_failure", text)

    def test_preflight_cli_is_read_only_and_names_each_repository(self):
        self.remove_policy("alpha")
        manifest = self.manifest([{"id": "a1", "repository": "alpha", "title": "x"},
                                  {"id": "b1", "repository": "beta", "title": "y"}])
        out = io.StringIO()
        with redirect_stdout(out):
            rc = wr.main(["preflight", str(manifest), "--no-access-check", "--allow-mutation"])
        self.assertEqual(rc, 1)
        self.assertIn("a1 [alpha] awaiting MUTATION_AUTHORIZATION_REQUIRED", out.getvalue())
        self.assertIn("b1 [beta] authorized", out.getvalue())
        self.assertFalse(self.launched())
        self.assertFalse((self.state / "workspaces" / "ws.json").exists())

    def test_denied_and_uncommitted_policies_are_not_authority(self):
        self.set_policy("alpha", commit=False)
        self.remove_policy("beta")
        # An untracked policy granting everything in the checkout does not count until committed and pinned.
        stage_mutation_policy(self.repos["beta"][0], push_branch=True, create_pr=True)
        git("-C", str(self.repos["beta"][0]), "rm", "-q", "--cached", ".pi/autonomy.yaml")
        manifest = self.manifest([{"id": "a1", "repository": "alpha", "title": "x"},
                                  {"id": "b1", "repository": "beta", "title": "y"},
                                  {"id": "r1", "repository": "gamma", "title": "read-only audit", "mutation": "none"}])
        verdicts = wr.preflight(manifest=manifest, allow_mutation=True)
        rows = {r["id"]: r for r in verdicts["tasks"]}
        self.assertEqual((rows["a1"]["status"], rows["a1"]["missing"]), ("awaiting", ["commit"]))
        self.assertEqual((rows["b1"]["status"], rows["b1"]["missing"]), ("awaiting", ["create_worktrees", "commit"]))
        self.assertEqual(rows["r1"]["status"], "not_required")
        beta = next(r for r in verdicts["approval_requests"] if r["repository"] == "beta")
        self.assertTrue(beta["uncommitted_policy"])
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            wr.launch(manifest=manifest, task_id="a1", allow_mutation=True, access_check=None, pi_executable=str(self.pi))
        self.assertEqual(ctx.exception.code, "MUTATION_AUTHORIZATION_REQUIRED")
        self.assertFalse(self.launched())

    def test_a_granting_policy_still_needs_the_operator_flag(self):
        manifest = self.manifest([{"id": "a1", "repository": "alpha", "title": "x"}])
        result = self.schedule(manifest, allow_mutation=False)
        self.assertEqual((self.states(result)["a1"], result["status"]), ("awaiting_authorization", "AWAITING_AUTHORIZATION"))
        self.assertEqual(result["approval_requests"][0]["code"], "MUTATION_NOT_AUTHORIZED")
        self.assertIn("Rerun with --allow-mutation", ws.render(result, manifest))
        self.assertFalse(self.launched())
        with redirect_stdout(io.StringIO()):
            rc = ws.main([str(manifest), "--no-access-check", "--pi", str(self.pi), "--poll-seconds", "0.05"])
        self.assertEqual(rc, 3)

    def test_a_repository_blocker_is_still_a_refusal_not_awaiting_authorization(self):
        manifest = self.manifest([{"id": "a1", "repository": "alpha", "title": "x"},
                                  {"id": "b1", "repository": "beta", "title": "y", "depends_on": ["a1"]}])
        data = yaml.safe_load(manifest.read_text())
        data["repositories"][0]["pinned_commit"] = "0" * 40  # not in the checkout: WORKSPACE_PIN_MISSING
        manifest.write_text(yaml.safe_dump(data))
        result = self.schedule(manifest)
        self.assertEqual(self.states(result), {"a1": "refused", "b1": "blocked_by_failure"})
        self.assertEqual(result["status"], "ATTENTION")
        self.assertEqual(result["approval_requests"], [])

    def test_the_garden_schedule_preview_holds_unauthorized_tasks(self):
        self.remove_policy("alpha")
        manifest = self.manifest([{"id": "a1", "repository": "alpha", "title": "x"},
                                  {"id": "b1", "repository": "beta", "title": "y"}])
        preview = gc.Controls(manifest, 1).act({"action": "schedule", "allow_mutation": True})
        self.assertEqual((preview["preview"]["dispatch"], preview["preview"]["awaiting_authorization"]), (["b1"], ["a1"]))

    def test_a_committed_policy_never_widens_to_publication(self):
        verdict = wa.check({"id": "t", "kind": "implementation"},
                           {"id": "alpha", "repository": "acme/alpha", "resolved_path": str(self.repos["alpha"][0]),
                            "pinned_commit": self.repos["alpha"][1]}, allow_mutation=True)
        self.assertEqual((verdict["status"], verdict["required"]), ("authorized", ["create_worktrees", "commit"]))
        line = wa.objective_line(verdict)
        self.assertIn("Push, pull requests, mark-ready, merge and deployment are not authorized", line)


class RecoveryTest(AuthorizationFixture):
    def test_observed_failure_is_awaiting_and_an_approved_attempt_recovers_it(self):
        for name in ("alpha", "beta", "delta"):
            self.remove_policy(name)
        manifest = self.scientific_review()
        for task_id in UPSTREAM:
            self.legacy_block(manifest, task_id)
        first = self.schedule(manifest)
        states = self.states(first)
        # Before this change both were "failed" and the five dependents "blocked_by_failure".
        self.assertEqual({t: states[t] for t in UPSTREAM}, dict.fromkeys(UPSTREAM, "awaiting_authorization"))
        self.assertEqual({t: states[t] for t in DOWNSTREAM}, dict.fromkeys(DOWNSTREAM, "waiting_for_authorization"))
        self.assertEqual(first["status"], "AWAITING_AUTHORIZATION")
        self.assertEqual(first["session"]["dispatched"], ["crt-docs"])
        blocked = next(r for r in first["tasks"] if r["id"] == "dp-handoff-controls")
        self.assertEqual((blocked["run_status"], blocked["run_blocker_code"]), ("BLOCKED", "HUMAN_MUTATION_APPROVAL_REQUIRED"))
        old_execution = self.execution("dp-handoff-controls").read_bytes()
        old_run = (self.state / "runs" / f"{blocked['run_id']}.json").read_bytes()

        # A human approves beta through the Garden's guarded action, commits the policy and re-pins.
        controls = gc.Controls(manifest, 1)
        preview = controls.act({"action": "approve_mutation", "repository": "beta"})
        self.assertEqual((preview["stage"], preview["preview"]["grants"]), ("preview", ["create_worktrees", "commit"]))
        done = controls.act({"action": "approve_mutation", "repository": "beta", "confirm": preview["confirm"]})
        self.assertEqual(done["status"], "POLICY_UPDATED")
        pending = {r["id"]: r for r in wr.preflight(manifest=manifest, allow_mutation=True)["tasks"]}
        self.assertEqual(pending["dp-handoff-controls"]["status"], "awaiting", "an uncommitted approval authorizes nothing")
        beta = str(self.repos["beta"][0])
        git("-C", beta, "config", "user.name", "t")  # CI runners have no global identity
        git("-C", beta, "config", "user.email", "t@example.com")
        commit = controls.act({"action": "commit_policy", "repository": "beta"})
        committed = controls.act({"action": "commit_policy", "repository": "beta", "confirm": commit["confirm"]})
        self.assertEqual(committed.get("status"), "POLICY_COMMITTED", committed)
        self.repos["beta"] = (self.repos["beta"][0], git("-C", str(self.repos["beta"][0]), "rev-parse", "HEAD"))
        manifest = self.scientific_review()

        second = self.schedule(manifest)
        states = self.states(second)
        self.assertEqual(second["session"]["new_attempts"], ["dp-handoff-controls"])
        self.assertEqual(second["session"]["dispatched"], ["dp-handoff-controls"])  # crt-docs is not run again
        self.assertEqual(states["dp-handoff-controls"], "partial")
        self.assertEqual(states["mm-schema-sync"], "awaiting_authorization")  # alpha was not approved
        self.assertEqual(states["crt-sep-import"], "waiting_for_readiness")
        row = next(r for r in second["tasks"] if r["id"] == "dp-handoff-controls")
        self.assertEqual(row["attempt"], 2)
        self.assertEqual(row["previous_attempts"], [{"attempt": 1, "execution_id": "ws--dp-handoff-controls",
                                                     "run_id": blocked["run_id"], "run_status": "BLOCKED",
                                                     "run_blocker_code": "HUMAN_MUTATION_APPROVAL_REQUIRED"}])
        self.assertNotEqual(row["run_id"], blocked["run_id"])
        # The blocked attempt's provenance is untouched; the new one has its own execution.
        # (status reconciliation refreshes only the execution record's updated_at)
        unchanged = json.loads(self.execution("dp-handoff-controls").read_text())
        self.assertEqual({**unchanged, "updated_at": None}, {**json.loads(old_execution), "updated_at": None})
        self.assertEqual((self.state / "runs" / f"{blocked['run_id']}.json").read_bytes(), old_run)
        new = hr.load_record(self.execution("dp-handoff-controls", 2))
        self.assertIn("Mutation authorization (checked by PICO before launch)", new["objective"])
        docs = next(r for r in second["tasks"] if r["id"] == "crt-docs")
        self.assertEqual(docs["launches"], 1)
        record = wr.load_workspace(wr.workspace_record_path("ws"))
        self.assertEqual(record["tasks"]["dp-handoff-controls"]["authorization"]["policy_source"], "committed")
        # Every attempt's run still counts against the budget.
        self.assertEqual(usage.workspace_usage(self.state / "runs", record)["runs"], 4)
        # The approved policy grants local mutation only.
        policy = yaml.safe_load((self.repos["beta"][0] / ".pi" / "autonomy.yaml").read_text())
        self.assertEqual(wa.granted_beyond_local(policy), [])

    def test_a_run_that_asks_again_under_the_same_authorization_is_not_looped(self):
        os.environ["FAKE_PI_BEHAVIOR_alpha"] = "blocked"
        manifest = self.manifest([{"id": "a1", "repository": "alpha", "title": "x"},
                                  {"id": "b1", "repository": "beta", "title": "y", "depends_on": ["a1"]}])
        first = self.schedule(manifest)
        self.assertEqual(self.states(first), {"a1": "awaiting_authorization", "b1": "waiting_for_authorization"})
        self.assertEqual(first["repeated_authorization_stops"], ["a1"])
        self.assertEqual(first["approval_requests"], [])
        second = self.schedule(manifest)
        self.assertEqual(second["session"]["dispatched"], [])
        self.assertEqual(next(r for r in second["tasks"] if r["id"] == "a1")["launches"], 1)
        self.assertIn("is not retried under the same authorization", ws.render(second, manifest))

    def test_a_blocked_run_that_changed_something_stays_a_failure(self):
        os.environ["FAKE_PI_BEHAVIOR_alpha"] = "blocked"
        manifest = self.manifest([{"id": "a1", "repository": "alpha", "title": "x"},
                                  {"id": "b1", "repository": "beta", "title": "y", "depends_on": ["a1"]}])
        first = self.schedule(manifest)
        run_path = self.state / "runs" / f"{first['tasks'][0]['run_id']}.json"
        run = json.loads(run_path.read_text())
        run["parent_orchestration"]["mutations"]["code"] = 1
        run_path.write_text(json.dumps(run))
        again = self.schedule(manifest)
        self.assertEqual(self.states(again), {"a1": "failed", "b1": "blocked_by_failure"})
        self.assertEqual(again["status"], "ATTENTION")

    def test_other_blocker_codes_stay_failures(self):
        os.environ["FAKE_PI_BEHAVIOR_alpha"] = "blocked"
        os.environ["FAKE_PI_BLOCKER"] = "TESTS_FAILED"
        manifest = self.manifest([{"id": "a1", "repository": "alpha", "title": "x"},
                                  {"id": "b1", "repository": "beta", "title": "y", "depends_on": ["a1"]}])
        self.assertEqual(self.states(self.schedule(manifest)), {"a1": "failed", "b1": "blocked_by_failure"})


class GardenApprovalTest(AuthorizationFixture):
    def test_approval_creates_a_policy_that_denies_everything_else(self):
        self.remove_policy("alpha")
        manifest = self.manifest([{"id": "a1", "repository": "alpha", "title": "x"}])
        controls = gc.Controls(manifest, 1)
        preview = controls.act({"action": "approve_mutation", "repository": "alpha"})
        self.assertTrue(preview["preview"]["creates_file"])
        self.assertEqual(preview["preview"]["never_granted"], list(wa.NEVER_GRANTED))
        stale = controls.act({"action": "approve_mutation", "repository": "alpha", "confirm": "0" * 64})
        self.assertEqual(stale["error_code"], "CONTROL_PREVIEW_STALE")
        controls.act({"action": "approve_mutation", "repository": "alpha", "confirm": preview["confirm"]})
        policy = yaml.safe_load((self.repos["alpha"][0] / ".pi" / "autonomy.yaml").read_text())
        granted = sorted(name for name, value in policy["permissions"].items() if value is True)
        self.assertEqual(granted, ["commit", "create_worktrees"])
        self.assertFalse(policy["permissions"]["merge"]["enabled"])
        again = controls.act({"action": "approve_mutation", "repository": "alpha"})
        self.assertEqual(again["error_code"], "POLICY_UNCHANGED")
        record = wr.load_workspace(wr.workspace_record_path("ws"))
        self.assertEqual(record["garden_actions"][-2]["granted"], ["create_worktrees", "commit"])

    def test_approval_of_an_existing_policy_changes_only_the_missing_lines(self):
        self.set_policy("alpha", create_worktrees=False, commit=False, push_branch=True)
        manifest = self.manifest([{"id": "a1", "repository": "alpha", "title": "x"}])
        before = (self.repos["alpha"][0] / ".pi" / "autonomy.yaml").read_text()
        controls = gc.Controls(manifest, 1)
        preview = controls.act({"action": "approve_mutation", "repository": "alpha"})
        self.assertEqual(preview["preview"]["already_granted_beyond_local"], ["push_branch"])
        changed = [line for line in preview["preview"]["diff"].splitlines() if line[:1] in "+-" and line[:3] not in {"+++", "---"}]
        self.assertEqual(sorted(changed), ["+  commit: true", "+  create_worktrees: true",
                                           "-  commit: false", "-  create_worktrees: false"])
        controls.act({"action": "approve_mutation", "repository": "alpha", "confirm": preview["confirm"]})
        after = (self.repos["alpha"][0] / ".pi" / "autonomy.yaml").read_text()
        self.assertEqual(after, before.replace("create_worktrees: false", "create_worktrees: true")
                         .replace("  commit: false", "  commit: true"))

    def test_the_garden_view_shows_the_request_and_the_states(self):
        self.remove_policy("beta")
        manifest = self.manifest([{"id": "b1", "repository": "beta", "title": "y"},
                                  {"id": "g1", "repository": "gamma", "title": "z", "depends_on": ["b1"]}])
        self.schedule(manifest)
        view = watch.workspace_view(self.state, manifest=manifest)
        self.assertEqual(view["status"], "AWAITING_AUTHORIZATION")
        tasks = {t["id"]: t for repo in view["repositories"] for t in repo["tasks"]}
        self.assertEqual((tasks["b1"]["state"], tasks["g1"]["state"]), ("awaiting_authorization", "waiting_for_authorization"))
        self.assertEqual(tasks["b1"]["authorization"], {"status": "awaiting", "code": "MUTATION_AUTHORIZATION_REQUIRED",
                                                        "missing": ["commit", "create_worktrees"]})
        request = view["approval_requests"][0]
        self.assertEqual((request["repository"], request["github"], request["tasks"], request["policy_source"]),
                         ("beta", "acme/beta", ["b1"], "default"))
        self.assertEqual(view["sessions"][0]["awaiting_authorization"], ["b1"])


if __name__ == "__main__":
    unittest.main()
