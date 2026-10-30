"""Workspace setup serves builder previews and exclusive saves behind the Garden token."""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from test_workspace_init import make_repo  # noqa: E402
import watch  # noqa: E402
import workspace_setup  # noqa: E402


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.a = self.root / "alpha"
        self.b = self.root / "beta"
        self.head_a = make_repo(self.a, {"origin": "https://github.com/example/alpha.git"})
        self.head_b = make_repo(self.b, {"origin": "https://github.com/example/beta.git"})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), watch.make_handler(self.root, None))
        self.setup = workspace_setup.SetupControls(self.root, self.server.server_port, state=self.root / "state",
            github=lambda name: {"repository": name, "default_branch": "main", "default_head": "c" * 40},
            access_check=lambda name: {"status": "ok", "blocker_code": None, "detail": None, "private": False})
        self.server.RequestHandlerClass = watch.make_handler(self.root, None, port=self.server.server_port,
                                                             setup=self.setup)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, method, path, body=None, *, token=True, origin=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=30)
        headers = {"Host": f"127.0.0.1:{self.server.server_port}"}
        if token: headers["X-Pico-Token"] = self.setup.token
        if method == "POST":
            headers["Content-Type"] = "application/json"
            if origin: headers["Origin"] = f"http://127.0.0.1:{self.server.server_port}"
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read())
        conn.close()
        return response.status, data

    def spec(self):
        return {"version": 1, "root": str(self.root), "id": "research", "goal": "Review two repositories",
                "max_parallel": 1, "repositories": [
                    {"id": "alpha", "repository": "example/alpha", "path": str(self.a),
                     "task": {"title": "Review alpha"}},
                    {"id": "beta", "repository": "example/beta", "path": str(self.b),
                     "task": {"title": "Review beta"}}]}

    def test_discovery_preview_save_and_stale_binding(self):
        status, data = self.request("GET", "/api/setup")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["candidates"]), 2)
        self.assertEqual(data["candidates"][0]["dirty"], False)
        spec = self.spec()
        status, preview = self.request("POST", "/api/setup", {"action": "preview", "spec": spec})
        self.assertEqual((status, preview["stage"]), (200, "preview"))
        self.assertEqual(preview["manifest"]["repositories"][0]["pinned_commit"], self.head_a)
        path = self.root / "research.workspace.yaml"
        self.assertFalse(path.exists())
        changed = {**spec, "goal": "Changed after preview"}
        status, refused = self.request("POST", "/api/setup", {"action": "save", "spec": changed,
                                                                   "confirm": preview["confirm"]})
        self.assertEqual((status, refused["error_code"]), (409, "CONTROL_PREVIEW_STALE"))
        self.assertFalse(path.exists())
        status, saved = self.request("POST", "/api/setup", {"action": "save", "spec": spec,
                                                                 "confirm": preview["confirm"]})
        self.assertEqual((status, saved["stage"]), (200, "saved"))
        self.assertIn("Review two repositories", path.read_text())
        status, refused = self.request("POST", "/api/setup", {"action": "save", "spec": spec,
                                                                   "confirm": preview["confirm"]})
        self.assertEqual((status, refused["error_code"]), (409, "WORKSPACE_INIT_OUTPUT_EXISTS"))

    def test_token_origin_and_root_are_required(self):
        for path in ("/api/setup", "/api/runs"):
            status, data = self.request("GET", path, token=False)
            self.assertEqual((status, data["error_code"]), (403, "CONTROL_TOKEN_INVALID"))
        status, data = self.request("POST", "/api/setup", {"action": "preview", "spec": self.spec()}, origin=False)
        self.assertEqual((status, data["error_code"]), (403, "CONTROL_ORIGIN_REJECTED"))
        spec = {**self.spec(), "root": "/tmp"}
        status, data = self.request("POST", "/api/setup", {"action": "preview", "spec": spec})
        self.assertEqual((status, data["error_code"]), (400, "WORKSPACE_INIT_ROOT_CHANGED"))

    def test_import_and_reopen_existing_manifest_read_only(self):
        spec = self.spec()
        _, preview = self.request("POST", "/api/setup", {"action": "preview", "spec": spec})
        imported = self.root / "existing.yaml"
        imported.write_text(preview["text"])
        record_dir = self.root / "state" / "workspaces"
        record_dir.mkdir(parents=True)
        (record_dir / "research.json").write_text(json.dumps({"record_version": 1, "workspace_id": "research",
                                                             "evidence_bindings": watch.workspace_run.evidence_bindings(imported),
                                                             "evidence_pins": watch.workspace_run.evidence_pins(imported),
                                                             "tasks": {"alpha": {"observed": {"readiness": "implemented"}}}}))
        status, discovered = self.request("GET", "/api/setup")
        self.assertEqual(status, 200)
        self.assertIn("existing.yaml", discovered["manifests"])
        before = imported.read_bytes()
        status, opened = self.request("GET", "/api/setup?manifest=existing.yaml")
        self.assertEqual(status, 200)
        self.assertTrue(opened["read_only"])
        self.assertEqual(opened["workspace"]["workspace_id"], "research")
        self.assertTrue(opened["workspace"]["record_found"])
        self.assertEqual(opened["workspace"]["repositories"][0]["tasks"][0]["readiness"], "implemented")
        self.assertEqual(opened["repositories"][0]["pinned_commit"], self.head_a)
        self.assertEqual(opened["repositories"][0]["access"]["status"], "ok")
        self.assertEqual(opened["repositories"][0]["branch"], "main")
        self.assertFalse(opened["repositories"][0]["dirty"])
        self.assertEqual(imported.read_bytes(), before)

    def test_import_rejects_traversal_symlink_and_invalid_content(self):
        (self.root / "outside.yaml").symlink_to("/etc/passwd")
        (self.root / "bad.yaml").write_text("not a workspace: true\n")
        status, listing = self.request("GET", "/api/setup")
        self.assertEqual(status, 200)
        self.assertNotIn("outside.yaml", listing["manifests"])
        for path, expected in (("/api/setup?manifest=..%2Foutside.yaml", "WORKSPACE_IMPORT_NAME_INVALID"),
                               ("/api/setup?manifest=outside.yaml", "WORKSPACE_IMPORT_UNAVAILABLE"),
                               ("/api/setup?manifest=bad.yaml", "WORKSPACE_IMPORT_INVALID")):
            status, data = self.request("GET", path)
            self.assertIn(status, {400, 404, 422})
            self.assertEqual(data["error_code"], expected)
        status, data = self.request("GET", "/api/setup?manifest=bad.yaml", token=False)
        self.assertEqual((status, data["error_code"]), (403, "CONTROL_TOKEN_INVALID"))

    def test_import_reports_dirty_checkout_and_missing_pin_without_writes(self):
        _, preview = self.request("POST", "/api/setup", {"action": "preview", "spec": self.spec()})
        imported = self.root / "stale.yaml"
        imported.write_text(preview["text"].replace(self.head_a, "f" * 40, 1))
        (self.a / "local-change.txt").write_text("uncommitted work\n")
        before = imported.read_bytes()
        status, opened = self.request("GET", "/api/setup?manifest=stale.yaml")
        self.assertEqual(status, 200, opened)
        self.assertTrue(opened["repositories"][0]["dirty"])
        self.assertEqual(opened["repositories"][0]["blocker_code"], "WORKSPACE_PIN_MISSING")
        self.assertIn("WORKSPACE_PIN_MISSING", [item["code"] for item in opened["blockers"]])
        self.assertEqual(imported.read_bytes(), before)


if __name__ == "__main__": unittest.main()
