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
        self.setup = workspace_setup.SetupControls(self.root, self.server.server_port,
            github=lambda name: {"repository": name, "default_branch": "main", "default_head": "c" * 40})
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


if __name__ == "__main__": unittest.main()
