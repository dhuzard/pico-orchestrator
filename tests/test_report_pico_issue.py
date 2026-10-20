import sys
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import report_pico_issue


class FakeGithub:
    def __init__(self, resolved="dhuzard/pico-orchestrator"):
        self.resolved = resolved
        self.calls = []

    def repo_view(self, *, repo):
        self.calls.append(("repo_view", repo))
        return {"nameWithOwner": self.resolved}

    def create_issue(self, title, body, *, repo):
        self.calls.append(("create_issue", repo, title, body))
        return {"number": 123, "url": "https://github.com/dhuzard/pico-orchestrator/issues/123"}


class ReportPicoIssueTests(unittest.TestCase):
    def test_preview_runs_outside_a_git_checkout_without_github(self):
        with tempfile.TemporaryDirectory() as directory:
            body_file = Path(directory) / "report.md"
            body_file.write_text("Report from another project\n", encoding="utf-8")
            script = Path(report_pico_issue.__file__)
            result = subprocess.run([sys.executable, str(script), "--title", "Bug", "--body-file", str(body_file)],
                                    cwd=directory, capture_output=True, text=True, check=True)
            preview = json.loads(result.stdout)
            self.assertEqual(preview["plan"]["repository"], "dhuzard/pico-orchestrator")
            self.assertEqual(preview["status"], "DRY_RUN")

    def test_preview_and_apply_use_only_the_canonical_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            body_file = Path(directory) / "report.md"
            body_file.write_text("The watch command lost --workspace.\n", encoding="utf-8")
            adapter = FakeGithub()
            preview = report_pico_issue.report(title="Watch bug", body_file=body_file, adapter=adapter)
            self.assertEqual(preview["status"], "DRY_RUN")
            self.assertEqual(adapter.calls, [])
            result = report_pico_issue.report(title="Watch bug", body_file=body_file,
                                              apply=True, plan_hash=preview["plan_hash"], adapter=adapter)
            self.assertEqual(result["status"], "COMPLETED")
            self.assertEqual(adapter.calls, [
                ("repo_view", "dhuzard/pico-orchestrator"),
                ("create_issue", "dhuzard/pico-orchestrator", "Watch bug",
                 "The watch command lost --workspace.\n"),
            ])

    def test_changed_report_and_wrong_destination_fail_before_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            body_file = Path(directory) / "report.md"
            body_file.write_text("Original report\n", encoding="utf-8")
            adapter = FakeGithub()
            preview = report_pico_issue.report(title="Bug", body_file=body_file, adapter=adapter)
            body_file.write_text("Changed report\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "changed since preview"):
                report_pico_issue.report(title="Bug", body_file=body_file, apply=True,
                                         plan_hash=preview["plan_hash"], adapter=adapter)
            self.assertEqual(adapter.calls, [])
            adapter = FakeGithub(resolved="another/repository")
            with self.assertRaisesRegex(ValueError, "canonical repository"):
                report_pico_issue.report(title="Bug", body_file=body_file, apply=True,
                                         plan_hash=report_pico_issue.report(title="Bug", body_file=body_file)["plan_hash"],
                                         adapter=adapter)
            self.assertEqual(adapter.calls, [("repo_view", "dhuzard/pico-orchestrator")])


if __name__ == "__main__":
    unittest.main()
