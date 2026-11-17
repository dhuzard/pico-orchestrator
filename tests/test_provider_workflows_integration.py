"""Optional real-plugin tests against a local fake provider; no provider account needed.

Set PICO_TEST_SUBAGENT_EXTENSION to the installed pi-subagent-workflows
extension/index.ts and PICO_TEST_CLAUDE_BRIDGE_EXTENSION to an installed
extension registering AskClaude: either a legacy combined bridge or the newer
pi-claude-bridge-askclaude companion. Pi, Node and (for the bridge) Claude Code must
be on PATH. Tests skip individually when their plugin is not configured.
Set PICO_TEST_PROVIDER_TIMEOUT=1 to additionally run the real hard-timeout
path, which takes at least 60 seconds because the plugin clamps the timeout.

Example (use absolute paths appropriate to your installation):
  PICO_TEST_SUBAGENT_EXTENSION=/absolute/path/pi-subagent-workflows/extension/index.ts \\
  PICO_TEST_CLAUDE_BRIDGE_EXTENSION=/absolute/path/askclaude-extension/src/index.ts \\
  PICO_TEST_PROVIDER_TIMEOUT=1 python3 -m unittest discover -s tests \\
    -p test_provider_workflows_integration.py -v

All credentials are fixed test strings. The gateway's upstream connection is
an in-memory fake, not an HTTP connection to either provider. The child HOME,
provider config, and authentication directories are temporary and isolated.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import budget_ledger as ledger  # noqa: E402
import provider_budget as gateway  # noqa: E402


class FakeResponse(io.BytesIO):
    status = 200

    def getheader(self, name, default=""):
        return "text/event-stream" if name == "Content-Type" else default


def _events(events):
    return b"\n\n".join(b"data: " + json.dumps(event).encode() for event in events) + b"\n\n"


def _openai_response(tool=None):
    if tool:
        name, arguments = tool
        item = {"id": "fc_test", "call_id": "call_test", "type": "function_call",
                "name": name, "arguments": json.dumps(arguments)}
        initial = {**item, "arguments": ""}
    else:
        item = {"id": "msg_test", "type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "OK", "annotations": []}]}
        initial = {**item, "content": []}
    events = [
        {"type": "response.created", "response": {"id": "resp_test", "model": "gpt-6-luna",
            "status": "in_progress", "output": []}},
        {"type": "response.output_item.added", "output_index": 0, "item": initial},
    ]
    if tool:
        events.append({"type": "response.function_call_arguments.done", "output_index": 0,
                       "arguments": item["arguments"]})
    else:
        events.extend([
            {"type": "response.content_part.added", "output_index": 0, "content_index": 0,
             "part": {"type": "output_text", "text": "", "annotations": []}},
            {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "OK"},
        ])
    usage = {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13,
             "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}
    events.extend([
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": {"id": "resp_test", "model": "gpt-6-luna",
            "status": "completed", "output": [item], "usage": usage}},
    ])
    return _events(events)


def _anthropic_response():
    events = [
        {"type": "message_start", "message": {"id": "msg_test", "type": "message", "role": "assistant",
            "model": "claude-opus-5-5", "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 0,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 3}},
        {"type": "message_stop"},
    ]
    return b"\n\n".join(("event: " + event["type"] + "\ndata: " + json.dumps(event)).encode()
                        for event in events) + b"\n\n"


def _model(provider, model_id, api, context, rates):
    return {"provider": provider, "id": model_id, "api": api, "context_window": context,
            "max_output_tokens": 64, "prices": rates, "evidence": "offline fixture"}


class RealPluginOfflineTest(unittest.TestCase):
    def _extension(self, variable):
        path = os.environ.get(variable)
        if not path:
            self.skipTest(f"set {variable} to opt into the installed plugin fixture")
        if not Path(path).is_absolute() or not Path(path).is_file():
            self.fail(f"{variable} must name an existing absolute extension path")
        if not shutil.which("pi"):
            self.skipTest("Pi must be on PATH")
        return path

    def _run(self, extension, *, bridge=False, timeout=False):
        with tempfile.TemporaryDirectory(prefix="pico-provider-offline-") as tmp:
            root = Path(tmp)
            ledger_path = root / "ledger.json"
            ledger.initialize(ledger_path, {"lifetime": {"tokens": 10000000, "cost_usd": "100", "requests": 10}})
            luna = _model("openai-codex", "gpt-6-luna", "openai-responses", 1050000,
                          {"input": "0.20", "output": "0.75", "cache_read": "0.02", "cache_write": "0.25"})
            models = [luna]
            if bridge:
                models.append(_model("anthropic", "claude-opus-5-5", "anthropic-messages", 1000000,
                                     {"input": "4", "output": "20", "cache_read": "0.2", "cache_write": "8"}))
            config = {"version": 1, "lead_model": "openai-codex/gpt-6-luna", "models": models, "extensions": []}
            calls = []
            calls_lock = threading.Lock()
            tool = ("AskClaude", {"prompt": "Say OK", "mode": "none", "model": "claude-opus-5-5", "isolated": True}) if bridge else (
                "spawn_subagent", {"task": "Reply with OK.", "label": "fixture", "tools": []})

            class Connection:
                def __init__(self, host):
                    self.host = host

                def request(self, method, path, body, headers):
                    with calls_lock:
                        calls.append((self.host, path, json.loads(body)))
                        self.number = len(calls)
                        reservations = ledger.inspect(ledger_path)["reservations"]
                        if len(reservations) < self.number:
                            raise AssertionError("upstream called before durable reservation")

                def getresponse(self):
                    # Hold the actual worker request past the plugin's 60 s minimum.
                    # Its timeout summary is handled by a separate gateway thread.
                    if timeout and self.number == 2:
                        time.sleep(65)
                    if self.host == "api.anthropic.com":
                        return FakeResponse(_anthropic_response())
                    return FakeResponse(_openai_response(tool if self.number == 1 else None))

                def close(self):
                    pass

            server = gateway.Gateway(config, ledger_path, "task", "attempt",
                                     {"OPENAI_API_KEY": "fake-openai", "ANTHROPIC_API_KEY": "fake-anthropic"},
                                     connection_factory=Connection)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                directory = root / "client"
                directory.mkdir()
                # Do not copy any ambient credentials or user configuration.
                parent = {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"}
                if bridge:
                    binary = shutil.which("claude")
                    if not binary:
                        self.skipTest("Claude Code must be on PATH for the bridge fixture")
                    parent["PI_ORCHESTRATOR_CLAUDE_BACKEND_IDENTITY"] = json.dumps({
                        "backend_executable": binary, "backend_version_output": "fixture", "backend_device": 0,
                        "backend_inode": 0})
                env = gateway.client_environment(config, directory, server, parent)
                # The only live listener allowed by this fixture is the local gateway.
                env.update(PI_OFFLINE="1", HTTPS_PROXY="http://127.0.0.1:1", HTTP_PROXY="http://127.0.0.1:1",
                           NO_PROXY="127.0.0.1", NODE_USE_ENV_PROXY="1")
                settings_path = directory / "pi" / "settings.json"
                settings = json.loads(settings_path.read_text())
                settings["subagentWorkflows"] = {"hardTimeoutMinutes": 0.003 if timeout else 1,
                    "softTimeoutMinutes": 1, "summarizeOnTimeout": True}
                settings_path.write_text(json.dumps(settings))
                guard = ROOT / "extensions" / "provider-budget-guard.ts"
                command = ["pi", "--no-extensions", "--no-approve", "--no-session", "-e", str(guard),
                           "-e", extension, "--print", "Execute the fixture task"]
                result = subprocess.run(command, env=env, cwd=directory, capture_output=True, text=True,
                                        timeout=80 if timeout else 30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("OK", result.stdout)
                # A synthetic final "OK" is not proof that the worker tool ran:
                # Pi can return a tool-not-found/error result to the lead and
                # still process the fake final response. Inspect the actual tool
                # result before checking the request count, with bounded output.
                final_input = calls[-1][2].get("input", []) if calls else []
                outputs = [item.get("output") for item in final_input
                           if isinstance(item, dict) and item.get("type") == "function_call_output"
                           and item.get("call_id") == "call_test"]
                diagnostic = repr(outputs)[:2000]
                self.assertEqual(len(outputs), 1, f"missing tool result; outputs={diagnostic}")
                if bridge:
                    self.assertEqual(outputs[0], "OK", f"AskClaude failed or is unavailable: {diagnostic}")
                else:
                    self.assertIn("OK", outputs[0], f"subagent failed or is unavailable: {diagnostic}")
                    if timeout:
                        self.assertIn("[hard timeout - session summarized]", outputs[0])
                    else:
                        self.assertIn("subagent_id:", outputs[0])
                self.assertEqual(len(calls), 4 if timeout else 3, f"gateway calls={len(calls)}, tool={diagnostic}")
                self.assertTrue(all(path == "/v1/responses" for host, path, _ in calls if host == "api.openai.com"))
                if bridge:
                    self.assertEqual([host for host, _, _ in calls],
                                     ["api.openai.com", "api.anthropic.com", "api.openai.com"])
                    self.assertEqual(calls[1][2]["model"], "claude-opus-5-5")
                elif timeout:
                    self.assertIn("Summarize an aborted subagent", json.dumps(calls[2][2]))
                else:
                    self.assertIn("Reply with OK", json.dumps(calls[1][2]))
                entries = list(ledger.inspect(ledger_path)["reservations"].values())
                self.assertEqual(len(entries), len(calls))
                if timeout:
                    self.assertEqual(sum(e["state"] in {"reserved", "uncertain"} for e in entries), 1)
                    self.assertEqual(sum(e["state"] == "settled" for e in entries), 3)
                else:
                    self.assertTrue(all(e["state"] == "settled" for e in entries))
            finally:
                server.shutdown()
                server.server_close()

    def test_real_spawn_subagent_and_final_response(self):
        self._run(self._extension("PICO_TEST_SUBAGENT_EXTENSION"))

    def test_real_askclaude_bridge_and_final_response(self):
        self._run(self._extension("PICO_TEST_CLAUDE_BRIDGE_EXTENSION"), bridge=True)

    def test_real_subagent_timeout_summary(self):
        if os.environ.get("PICO_TEST_PROVIDER_TIMEOUT") != "1":
            self.skipTest("set PICO_TEST_PROVIDER_TIMEOUT=1 to run the 60-second timeout fixture")
        self._run(self._extension("PICO_TEST_SUBAGENT_EXTENSION"), timeout=True)


if __name__ == "__main__":
    unittest.main()
