"""Read-only identity normalization for standing-rule shadow inputs."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import standing_snapshot as snap  # noqa: E402


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.repo = {"id": 101, "full_name": "acme/private-target", "private": True, "fork": False}
        self.issue = {"id": 303, "number": 7, "repository_url": "https://api.github.com/repos/acme/private-target",
                      "user": {"id": 404}, "title": "Correct a typo", "body": "Guide prose needs correction.",
                      "state": "open", "labels": [{"name": "autonomy:docs-prose"}]}
        self.calls = []

    def gh(self, args):
        self.calls.append(args)
        return copy.deepcopy(self.repo if len(self.calls) % 2 else self.issue)

    def test_observation_binds_ids_and_content_without_writes(self):
        observed = snap.observe_issue(self.gh, "acme/private-target", 7)
        self.assertEqual(self.calls, [["api", "repos/acme/private-target"],
                                      ["api", "repos/acme/private-target/issues/7"]])
        self.assertEqual(observed["repository"], {"id": 101, "name": "acme/private-target",
                                                  "private": True, "is_fork": False})
        self.assertEqual((observed["id"], observed["author_id"], observed["state"]), (303, 404, "OPEN"))
        self.assertFalse(observed["is_pull_request"])

    def test_mismatched_or_missing_identity_refuses(self):
        changes = [
            ("repo", "id", None), ("repo", "full_name", "other/target"),
            ("repo", "private", False), ("repo", "fork", True),
            ("issue", "id", None), ("issue", "number", 8),
            ("issue", "repository_url", "https://api.github.com/repos/other/target"),
            ("issue", "user", {"id": None}), ("issue", "body", None),
            ("issue", "labels", ["autonomy:docs-prose"]),
        ]
        for source, field, value in changes:
            with self.subTest(source=source, field=field):
                self.setUp()
                getattr(self, source)[field] = value
                with self.assertRaises(snap.SnapshotError):
                    snap.observe_issue(self.gh, "acme/private-target", 7)

    def test_pull_request_cannot_masquerade_as_issue(self):
        self.issue["pull_request"] = {"url": "https://api.github.com/repos/acme/private-target/pulls/7"}
        self.assertTrue(snap.observe_issue(self.gh, "acme/private-target", 7)["is_pull_request"])

    def test_api_errors_are_redacted(self):
        def unavailable(_):
            raise RuntimeError("credential value must not be reported")
        with self.assertRaisesRegex(snap.SnapshotError, "observation unavailable") as caught:
            snap.observe_issue(unavailable, "acme/private-target", 7)
        self.assertNotIn("credential value", str(caught.exception))

    def test_invalid_selector_makes_no_api_request(self):
        for repository, number in (("other/../target", 7), ("../target", 7),
                                   ("acme/private-target", True),
                                   ("acme/private-target", 0)):
            with self.assertRaises(snap.SnapshotError):
                snap.observe_issue(self.gh, repository, number)
        self.assertEqual(self.calls, [])
