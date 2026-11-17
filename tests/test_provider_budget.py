"""Offline HTTP transport tests: never contact or authenticate with a provider."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import io
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import budget_ledger as ledger
import provider_budget as gateway


def config(api="openai-responses"):
    anthropic = api == "anthropic-messages"
    return {"version": 1, "lead_model": "anthropic/claude-opus-5-5" if anthropic else "openai-codex/gpt-6-luna",
            "models": [{"provider": "anthropic" if anthropic else "openai-codex",
                        "id": "claude-opus-5-5" if anthropic else "gpt-6-luna", "api": api,
                        "context_window": 1000000 if anthropic else 1050000, "max_output_tokens": 64,
                        "prices": {"input": "4" if anthropic else "0.20", "output": "20" if anthropic else "0.75",
                                   "cache_read": "0.2" if anthropic else "0.02", "cache_write": "8" if anthropic else "0.25"},
                        "evidence": "official catalog 2026-11-17"}], "extensions": []}


class Response(io.BytesIO):
    status = 200

    def __init__(self, body, streaming=False):
        super().__init__(body)
        self.streaming = streaming

    def getheader(self, name, default=""):
        return ("text/event-stream" if self.streaming else "application/json") if name == "Content-Type" else default


class BudgetGatewayTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(gateway, "catalog_today", return_value=gateway.date(2026, 11, 17))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ledger.json"
        self.contract = config()
        self.calls = []
        self.body = json.dumps({"status": "completed", "usage": {"input_tokens": 10, "output_tokens": 3}}).encode()
        self.streaming = False
        self.upstream_status = 200
        ledger.initialize(self.path, {"lifetime": {"tokens": 2100128, "cost_usd": "10", "requests": 10}})
        owner = self

        class Connection:
            def request(self, method, path, body, headers):
                # Assert reservation exists DURING the transport operation.
                assert any(e["state"] == "reserved" for e in ledger.inspect(owner.path)["reservations"].values())
                owner.calls.append((path, json.loads(body), headers))

            def getresponse(self):
                response = Response(owner.body() if callable(owner.body) else owner.body, owner.streaming)
                response.status = owner.upstream_status
                return response

            def close(self):
                pass

        self.server = gateway.Gateway(self.contract, self.path, "task", "attempt", {"OPENAI_API_KEY": "upstream-test-key",
            "ANTHROPIC_API_KEY": "upstream-anthropic-test"}, connection_factory=lambda _host: Connection())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, payload=None, path="/openai/v1/responses", **extra_headers):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        client.request("POST", path, json.dumps(payload or {"model": "gpt-6-luna", "input": "hello", "max_output_tokens": 999999}),
                       {"Authorization": "Bearer " + self.server.token, "Content-Type": "application/json", **extra_headers})
        response = client.getresponse()
        result = response.status, response.read()
        client.close()
        return result

    def test_reserves_before_send_enforces_output_and_settles_complete_usage(self):
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.calls[0][1]["max_output_tokens"], 64)
        self.assertEqual(self.calls[0][2]["Authorization"], "Bearer upstream-test-key")
        entry = next(iter(ledger.inspect(self.path)["reservations"].values()))
        self.assertEqual(entry["reserved"]["tokens"], 1050064)
        self.assertEqual(entry["actual"]["tokens"], 13)
        self.assertEqual(entry["state"], "settled")

    def test_missing_receipt_keeps_full_reservation_and_blocks_before_next_send(self):
        self.body = b'{"status":"completed","usage":{"input_tokens":1}}'
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.request()[0], 429)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(e["state"] == "uncertain" for e in ledger.inspect(self.path)["reservations"].values()))

    def test_each_client_retry_reserves_fresh_even_with_same_idempotency_key(self):
        self.upstream_status = 503
        for _ in range(2):
            self.assertEqual(self.request(**{"Idempotency-Key": "same"})[0], 502)
        self.assertEqual(self.request(**{"Idempotency-Key": "same"})[0], 429)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(ledger.inspect(self.path)["reservations"]), 2)

    def test_concurrent_http_attempts_cannot_overbook(self):
        self.body = b'{}'
        with ThreadPoolExecutor(max_workers=5) as pool:
            statuses = list(pool.map(lambda _: self.request()[0], range(5)))
        self.assertEqual(statuses.count(200), 2)
        self.assertEqual(statuses.count(429), 3)
        self.assertEqual(len(self.calls), 2)

    def test_unsupported_content_models_tools_tiers_endpoints_never_send(self):
        for extra in ({"model": "unpriced"}, {"tools": [{"type": "web_search"}]}, {"service_tier": "priority"},
                      {"previous_response_id": "hidden"}, {"background": True},
                      {"input": [{"role": "user", "content": [{"type": "input_image", "image_url": "https://example.com"}]}]}):
            self.assertEqual(self.request({"model": "gpt-6-luna", "input": "hello", **extra})[0], 400)
        self.assertEqual(self.request(path="/v1/chat/completions")[0], 400)
        self.assertFalse(self.calls)
        self.assertFalse(ledger.inspect(self.path)["reservations"])

    def test_anthropic_cache_categories_and_terminal_event(self):
        replacement = config("anthropic-messages")
        ledger.rebind_empty_provider_contract(self.path, expected=gateway.contract_digest(self.server.config),
            replacement=gateway.contract_digest(replacement), reason="Offline fixture switches provider")
        self.server.config = replacement
        self.streaming = True
        self.body = b'\n'.join(b'data: ' + json.dumps(event).encode() for event in [
            {"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 0,
                "cache_read_input_tokens": 12, "cache_creation_input_tokens": 15}}},
            {"type": "message_delta", "usage": {"output_tokens": 3}}, {"type": "message_stop"}]) + b'\n\n'
        self.assertEqual(self.request({"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]},
                                     path="/anthropic/v1/messages")[0], 200)
        entry = next(iter(ledger.inspect(self.path)["reservations"].values()))
        self.assertEqual(entry["actual"]["tokens"], 40)
        self.assertEqual(entry["actual"]["cost_usd"], "0.000356")

    def test_truncated_stream_does_not_release_capacity(self):
        self.streaming = True
        self.body = b'data: {"type":"response.created","response":{"usage":{"input_tokens":1,"output_tokens":0}}}\n\n'
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(next(iter(ledger.inspect(self.path)["reservations"].values()))["state"], "uncertain")

    def test_credentials_and_all_runtime_auth_directories_are_isolated(self):
        directory = Path(self.temp.name) / "client"
        directory.mkdir()
        env = gateway.client_environment(self.contract, directory, self.server, {
            "HOME": "/original/home", "XDG_CONFIG_HOME": "/original/config", "CODEX_HOME": "/original/codex",
            "OPENAI_API_KEY": "real-secret", "ANTHROPIC_API_KEY": "real-secret", "ANTHROPIC_AUTH_TOKEN": "oauth",
            "GOOGLE_APPLICATION_CREDENTIALS": "/secrets", "GIT_AUTHOR_NAME": "author"})
        self.assertEqual(env["OPENAI_API_KEY"], self.server.token)
        self.assertEqual(env["ANTHROPIC_API_KEY"], self.server.token)
        self.assertEqual(env["GIT_AUTHOR_NAME"], "author")
        self.assertNotIn("real-secret", str(env))
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        for name in ("HOME", "XDG_CONFIG_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR"):
            self.assertTrue(Path(env[name]).is_relative_to(directory))

    def test_project_bridge_override_is_refused(self):
        directory = Path(self.temp.name)
        gateway.validate_project(directory)
        (directory / ".pi").mkdir()
        (directory / ".pi/claude-bridge.json").write_text('{}')
        with self.assertRaisesRegex(ledger.BudgetError, "project Claude bridge"):
            gateway.validate_project(directory)

    def test_idle_gateway_cannot_forward_after_explicit_empty_contract_rebind(self):
        replacement = config("anthropic-messages")
        ledger.rebind_empty_provider_contract(self.path, expected=gateway.contract_digest(self.contract),
            replacement=gateway.contract_digest(replacement), reason="Offline unavailable-model recovery")
        self.assertEqual(self.request()[0], 400)
        self.assertFalse(self.calls)
        self.assertFalse(ledger.inspect(self.path)["reservations"])

    def test_explicit_rebind_command_validates_config_and_preserves_lifetime_limits(self):
        from contextlib import redirect_stdout
        path = Path(self.temp.name) / "unused-ledger.json"
        replacement_path = Path(self.temp.name) / "replacement.json"
        replacement_path.write_text(json.dumps(config("anthropic-messages")))
        limits = {"lifetime": {"tokens": 8000000, "cost_usd": "30", "requests": 200, "launches": 6},
                  "attempt": {"cost_usd": "10"}}
        ledger.initialize(path, limits)
        original = gateway.contract_digest(self.contract)
        ledger.bind_provider_contract(path, original)
        output = io.StringIO()
        with redirect_stdout(output), mock.patch.dict(os.environ, {}, clear=True):
            result = gateway.main(["rebind-empty", "--config", str(replacement_path), "--ledger", str(path),
                "--expected-contract", original, "--reason", "Approved replacement after no-spend metadata preflight",
                "--attempt-cost-usd", "15"])
        self.assertEqual(result, 0)
        self.assertTrue(json.loads(output.getvalue())["ok"])
        data = ledger.inspect(path)
        self.assertEqual(data["limits"]["lifetime"], limits["lifetime"])
        self.assertEqual(data["limits"]["attempt"]["cost_usd"], "15")
        self.assertEqual(data["reservations"], {})
        self.assertEqual(len(data["provider_contract_history"]), 1)

    def test_oauth_credentials_and_launch_only_ledgers_cannot_enable_provider_mode(self):
        path = Path(self.temp.name) / "config.json"
        path.write_text(json.dumps(config()))
        with self.assertRaisesRegex(ledger.BudgetError, "CREDENTIAL_UNSUPPORTED"):
            gateway.validate_launch(path, self.path, {"OPENAI_API_KEY": "oauth-subscription-token"})
        launches = Path(self.temp.name) / "launches.json"
        ledger.initialize(launches, {"lifetime": {"launches": 2}})
        with self.assertRaisesRegex(ledger.BudgetError, "token and dollar ceilings"):
            gateway.validate_launch(path, launches, {"OPENAI_API_KEY": "sk-fake-key"})

    def test_anthropic_personal_api_keys_are_accepted_but_oauth_and_malformed_keys_are_refused(self):
        path = Path(self.temp.name) / "anthropic-config.json"
        path.write_text(json.dumps(config("anthropic-messages")))
        allowance = Path(self.temp.name) / "anthropic-ledger.json"
        ledger.initialize(allowance, {"lifetime": {"tokens": 2000000, "cost_usd": "10"}})
        for credential in ("sk-ant-api03-offline-fixture", "sk-ant-api-offline-fixture", "sk-ant-usr-offline-fixture"):
            self.assertEqual(gateway.validate_launch(path, allowance, {"ANTHROPIC_API_KEY": credential}),
                             config("anthropic-messages"))
        before = allowance.read_bytes()
        for credential in ("sk-ant-oat01-offline-fixture", "subscription-token", "sk-ant-svc-offline-fixture",
                           "sk-ant-usr-", "sk-ant-api03-", "sk-ant-api", "sk-ant-usr-fake token",
                           "sk-ant-usr-fake\n", "sk-ant-user-offline-fixture"):
            with self.subTest(credential_kind=credential.split("-")[:3]):
                with self.assertRaisesRegex(ledger.BudgetError, "CREDENTIAL_UNSUPPORTED") as refused:
                    gateway.validate_launch(path, allowance, {"ANTHROPIC_API_KEY": credential})
                self.assertNotIn(credential, str(refused.exception))
                self.assertEqual(allowance.read_bytes(), before)
        self.assertFalse(self.calls)
        self.assertEqual(ledger.inspect(allowance)["reservations"], {})

    def test_anthropic_personal_api_key_uses_x_api_key_header_only(self):
        replacement = config("anthropic-messages")
        ledger.rebind_empty_provider_contract(self.path, expected=gateway.contract_digest(self.server.config),
            replacement=gateway.contract_digest(replacement), reason="Offline personal API-key fixture")
        self.server.config = replacement
        self.server.credentials["ANTHROPIC_API_KEY"] = "sk-ant-usr-offline-fixture"
        self.body = json.dumps({"type": "message", "stop_reason": "end_turn", "usage": {"input_tokens": 10,
            "output_tokens": 3, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}).encode()
        self.assertEqual(self.request({"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]},
                                     path="/anthropic/v1/messages")[0], 200)
        self.assertEqual(self.calls[0][2]["x-api-key"], "sk-ant-usr-offline-fixture")
        self.assertNotIn("Authorization", self.calls[0][2])

    def test_catalog_rejects_understated_price_context_and_unknown_alias(self):
        path = Path(self.temp.name) / "config.json"
        for field, value in (("context_window", 1), ("id", "unknown-model"), ("prices", dict.fromkeys(gateway.PRICES, "0.000001"))):
            value_config = config()
            value_config["models"][0][field] = value
            path.write_text(json.dumps(value_config))
            with self.assertRaises(ledger.BudgetError):
                gateway.load_config(path)
        path.write_text(json.dumps(config()))
        self.assertEqual(gateway.load_config(path), config())
        with mock.patch.object(gateway, "catalog_today", return_value=gateway.date(2026, 11, 25)):
            with self.assertRaisesRegex(ledger.BudgetError, "new official-source review"):
                gateway.load_config(path)
            self.assertEqual(self.request()[0], 400)
            self.assertFalse(self.calls)
        gateway.validate_launch(path, self.path, {"OPENAI_API_KEY": "sk-fake-key"})
        changed = config()
        changed["models"][0]["max_output_tokens"] = 32
        path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ledger.BudgetError, "CONFLICT"):
            gateway.validate_launch(path, self.path, {"OPENAI_API_KEY": "sk-fake-key"})

    def test_real_claude_cli_nested_tool_turns_use_gateway(self):
        binary = shutil.which("claude")
        if binary is None:
            self.skipTest("Claude CLI optional; fake transport tests still run")
        replacement = config("anthropic-messages")
        ledger.rebind_empty_provider_contract(self.path, expected=gateway.contract_digest(self.server.config),
            replacement=gateway.contract_digest(replacement), reason="Offline fixture switches provider")
        self.server.config = replacement
        self.streaming = True
        def body():
            tool = len(self.calls) == 1
            block = ({"type": "tool_use", "id": "tool_test", "name": "Bash", "input": {}}
                     if tool else {"type": "text", "text": ""})
            delta = ({"type": "input_json_delta", "partial_json": json.dumps({"command": "printf fixture", "description": "Offline fixture"})}
                     if tool else {"type": "text_delta", "text": "OK"})
            events = [
                {"type": "message_start", "message": {"id": f"msg_{len(self.calls)}", "type": "message", "role": "assistant",
                    "model": "claude-opus-5-5", "content": [], "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}},
                {"type": "content_block_start", "index": 0, "content_block": block},
                {"type": "content_block_delta", "index": 0, "delta": delta},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use" if tool else "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 3}}, {"type": "message_stop"}]
            return b'\n\n'.join(('event: ' + e['type'] + '\ndata: ' + json.dumps(e)).encode() for e in events) + b'\n\n'
        self.body = body
        directory = Path(self.temp.name) / "claude-runtime"
        directory.mkdir()
        parent = dict(os.environ, PI_ORCHESTRATOR_CLAUDE_BACKEND_IDENTITY=json.dumps({
            "backend_executable": binary, "backend_version_output": "fixture", "backend_device": 0, "backend_inode": 0}))
        env = gateway.client_environment(self.server.config, directory, self.server, parent)
        # A project override must not replace the gateway transport.
        (directory / ".claude").mkdir()
        (directory / ".claude/settings.json").write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:1"}}))
        shim = json.loads(env["PI_ORCHESTRATOR_CLAUDE_BACKEND_IDENTITY"])["backend_executable"]
        result = subprocess.run([shim, "-p", "--setting-sources", "user,project", "--model", "claude-opus-5-5", "--tools", "Bash", "--allowedTools", "Bash",
                                 "--max-turns", "3", "--output-format", "json", "--no-session-persistence", "hello"],
                                env=env, cwd=directory, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["result"], "OK")
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(call[1]["max_tokens"] == 64 for call in self.calls))
        self.assertTrue(all(e["state"] == "settled" for e in ledger.inspect(self.path)["reservations"].values()))
        refused = subprocess.run([shim, "-p", "--settings", '{"env":{"ANTHROPIC_API_KEY":"escape"}}', "hello"],
                                 env=env, cwd=directory, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("PROVIDER_BUDGET_UNSUPPORTED", refused.stderr)
        self.assertEqual(len(self.calls), 2)

    def test_real_pi_lead_uses_gateway_without_live_provider(self):
        binary = Path(__file__).resolve().parents[1] / "node_modules" / ".bin" / "pi"
        if not binary.exists():
            self.skipTest("npm ci required for real Pi fixture")
        directory = Path(self.temp.name) / "pi-runtime"
        directory.mkdir()
        env = gateway.client_environment(self.contract, directory, self.server, os.environ)
        self.streaming = True
        item = {"id": "msg_test", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "OK", "annotations": []}]}
        self.body = b'\n\n'.join(b'data: ' + json.dumps(e).encode() for e in [
            {"type": "response.created", "response": {"id": "resp_test", "model": "gpt-6-luna", "status": "in_progress", "output": []}},
            {"type": "response.output_item.added", "output_index": 0, "item": {**item, "content": []}},
            {"type": "response.content_part.added", "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}},
            {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "OK"},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": {"id": "resp_test", "model": "gpt-6-luna", "status": "completed", "output": [item],
                "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13, "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}}}]) + b'\n\n'
        result = subprocess.run([str(binary), "--no-extensions", "--no-approve", "--no-session", "--no-tools", "--print", "hello"],
                                env=env, cwd=directory, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK", result.stdout)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(next(iter(ledger.inspect(self.path)["reservations"].values()))["state"], "settled")
        # pi-subagent-workflows creates a fresh SDK session, then separately
        # calls compat.completeSimple for its timeout summary. Neither call
        # uses lead extension hooks. Exercise both installed runtime paths.
        root = Path(__file__).resolve().parents[1]
        sdk = (root / "node_modules/@earendil-works/pi-coding-agent/dist/index.js").as_uri()
        compat = (root / "node_modules/@earendil-works/pi-ai/dist/compat.js").as_uri()
        script = f'''
          const sdk = await import({json.dumps(sdk)});
          const compat = await import({json.dumps(compat)});
          const runtime = await sdk.ModelRuntime.create({{authPath: process.env.PI_CODING_AGENT_DIR + '/auth.json',
              modelsPath: process.env.PI_CODING_AGENT_DIR + '/models.json', allowModelNetwork: false}});
          const model = runtime.getModel('openai-codex', 'gpt-6-luna');
          if (model.api !== 'openai-responses' || !model.baseUrl.startsWith('http://127.0.0.1:')) throw Error('bypass');
          const created = await sdk.createAgentSession({{cwd: process.cwd(), agentDir: process.env.PI_CODING_AGENT_DIR,
              model, noTools: 'all', sessionManager: sdk.SessionManager.inMemory()}});
          await created.session.prompt('worker fixture');
          created.session.dispose();
          const auth = await runtime.getAuth(model);
          const summary = await compat.completeSimple(model, {{systemPrompt: 'timeout summary fixture',
              messages: [{{role:'user', content:'hi', timestamp:Date.now()}}]}}, {{apiKey: auth.auth.apiKey}});
          if (summary.stopReason === 'error') throw Error(summary.errorMessage);
        '''
        result = subprocess.run(["node", "--input-type=module", "-e", script], env=env, cwd=directory,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(all(e["state"] == "settled" for e in ledger.inspect(self.path)["reservations"].values()))

    def test_native_pi_anthropic_high_uses_adaptive_thinking_and_exact_model(self):
        from test_provider_workflows_integration import _anthropic_response
        binary = Path(__file__).resolve().parents[1] / "node_modules/.bin/pi"
        if not binary.exists():
            self.skipTest("npm ci required for native Anthropic runtime fixture")
        replacement = config("anthropic-messages")
        ledger.rebind_empty_provider_contract(self.path, expected=gateway.contract_digest(self.server.config),
            replacement=gateway.contract_digest(replacement), reason="Offline native Anthropic high fixture")
        self.server.config = replacement
        self.streaming, self.body = True, _anthropic_response()
        directory = Path(self.temp.name) / "native-anthropic"
        directory.mkdir()
        env = gateway.client_environment(replacement, directory, self.server, os.environ)
        result = subprocess.run([str(binary), "--no-extensions", "--no-approve", "--no-session", "--no-tools",
                                 "--thinking", "high", "--print", "hello"], env=env, cwd=directory,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK", result.stdout)
        self.assertEqual(len(self.calls), 1)
        path, payload, _headers = self.calls[0]
        self.assertEqual(path, "/v1/messages")
        self.assertEqual(payload["model"], "claude-opus-5-5")
        self.assertEqual(payload["thinking"]["type"], "adaptive")
        self.assertEqual(payload["output_config"]["effort"], "high")
        self.assertEqual(payload["max_tokens"], 64)
        self.assertEqual(next(iter(ledger.inspect(self.path)["reservations"].values()))["state"], "settled")


if __name__ == "__main__":
    unittest.main()
