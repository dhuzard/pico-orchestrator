#!/usr/bin/env python3
"""Credential-owning, fail-closed HTTP admission for a controlled API pilot.

No subscription transport, upstream retry, redirect, tokenizer estimate or
post-hoc process kill is used to justify a bound. See provider-budget.md.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
from urllib.parse import urlsplit

import budget_ledger as ledger

CONFIG_ENV = "PICO_PROVIDER_BUDGET_CONFIG"
MAX_BODY = 16 * 1024 * 1024
MAX_RESPONSE = 64 * 1024 * 1024
APIS = {"openai-responses": ("api.openai.com", "/v1/responses", "OPENAI_API_KEY"),
        "anthropic-messages": ("api.anthropic.com", "/v1/messages", "ANTHROPIC_API_KEY")}
PRICES = {"input", "output", "cache_read", "cache_write"}
# Reviewed 2026-11-17 against official model/context/pricing pages linked in
# docs/provider-budget.md. Long-context rates, worst cache creation category.
# This deliberately short review window refuses a stale mutable model alias.
CATALOG_VALID_UNTIL = date(2026, 11, 24)
CATALOG = {
    "gpt-6-luna": ("openai-responses", 1050000, 128000, ("0.20", "0.75", "0.02", "0.25")),
    "gpt-6.1-sol": ("openai-responses", 1050000, 128000, ("4", "15", "0.20", "5")),
    "gpt-6-astra": ("openai-responses", 1050000, 128000, ("20", "75", "2", "25")),
    "claude-opus-5-5": ("anthropic-messages", 1000000, 128000, ("4", "20", "0.2", "8")),
}


def fail(message, code="PROVIDER_BUDGET_UNSUPPORTED"):
    raise ledger.BudgetError(code, message)


def positive(value):
    return type(value) is int and value > 0


def catalog_today():
    return datetime.now(timezone.utc).date()


def load_config(path):
    path = Path(path)
    if not path.is_absolute():
        fail("provider config must be an absolute path")
    try:
        if catalog_today() > CATALOG_VALID_UNTIL:
            fail("provider pricing/context catalog needs a new official-source review")
        config = json.loads(path.read_text())
        if (config.get("version") != 1 or set(config) - {"version", "lead_model", "models", "extensions"}
                or not isinstance(config["models"], list) or not config["models"]):
            fail("invalid provider contract")
        identities = set()
        for model in config["models"]:
            if set(model) != {"provider", "id", "api", "context_window", "max_output_tokens", "prices", "evidence"}:
                fail("model must explicitly pin API, context, output, prices and evidence")
            if (model["api"] not in APIS or model["provider"] not in {"openai", "openai-codex", "anthropic"}
                    or (model["provider"] == "anthropic") != (model["api"] == "anthropic-messages")
                    or not isinstance(model["id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", model["id"])
                    or not positive(model["context_window"]) or not positive(model["max_output_tokens"])
                    or model["max_output_tokens"] > model["context_window"]
                    or not isinstance(model["evidence"], str) or not model["evidence"].strip()):
                fail("invalid model contract")
            if set(model["prices"]) != PRICES:
                fail("all input/output/cache prices must be pinned")
            for price in model["prices"].values():
                if not isinstance(price, str) or not Decimal(price).is_finite() or Decimal(price) <= 0:
                    fail("prices must be positive decimal strings (USD/million)")
            reviewed = CATALOG.get(model["id"])
            if reviewed is None or reviewed[0] != model["api"]:
                fail("model is absent from the reviewed provider catalog")
            if model["context_window"] < reviewed[1] or model["max_output_tokens"] > reviewed[2]:
                fail("context bound cannot be lowered; output cannot exceed provider maximum")
            for name, minimum in zip(("input", "output", "cache_read", "cache_write"), reviewed[3], strict=True):
                if Decimal(model["prices"][name]) < Decimal(minimum):
                    fail("price is below the reviewed conservative ceiling")
            identity = (model["api"], model["id"])
            if identity in identities:
                fail("duplicate upstream model")
            identities.add(identity)
        if config["lead_model"] not in {m["provider"] + "/" + m["id"] for m in config["models"]}:
            fail("lead model must be explicitly pinned")
        extensions = config.get("extensions", [])
        if not isinstance(extensions, list) or any(not isinstance(p, str) or not Path(p).is_absolute()
                                                  or not Path(p).is_file() for p in extensions):
            fail("extensions must be reviewed existing absolute file paths")
        return config
    except (OSError, ValueError, TypeError, KeyError, InvalidOperation) as exc:
        fail(f"cannot read provider contract: {type(exc).__name__}")


def contract_digest(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_launch(config_path, ledger_path, env=None):
    config = load_config(config_path)
    if not ledger_path:
        fail("provider mode requires an existing aggregate ledger")
    env = os.environ if env is None else env
    for api in {m["api"] for m in config["models"]}:
        credential = env.get(APIS[api][2], "")
        if not credential:
            fail(f"API credential unavailable: {APIS[api][2]}", "PROVIDER_BUDGET_CREDENTIAL_UNAVAILABLE")
        # Personal `usr` API keys also authenticate through x-api-key. Keep
        # this structural allowlist narrow: OAuth `oat` and unverified key
        # kinds are not admitted. The provider still authenticates the key.
        supported_credential = (re.fullmatch(r"sk-ant-(?:api[0-9]*|usr)-[A-Za-z0-9_-]+", credential) is not None
                                if api == "anthropic-messages" else credential.startswith("sk-"))
        if not supported_credential:
            fail("only provider API-key credentials are supported; subscription/OAuth credentials are refused",
                 "PROVIDER_BUDGET_CREDENTIAL_UNSUPPORTED")
    limits = ledger.inspect(ledger_path)["limits"]
    if not any({"tokens", "cost_usd"} <= set(scope) for scope in limits.values()):
        fail("provider mode requires token and dollar ceilings in at least one shared scope")
    ledger.bind_provider_contract(ledger_path, contract_digest(config))
    return config


def validate_project(cwd):
    # Match pi-claude-bridge/src/config.ts loadConfig: it merges exactly this
    # cwd file independently of Pi's project-trust/--no-approve setting.
    if (Path(cwd) / ".pi" / "claude-bridge.json").exists():
        fail("project Claude bridge overrides are unsupported in budget mode")


def amounts(model, inputs, outputs):
    # Worst input category is also used at settlement: cache receipt omissions
    # cannot produce an optimistic dollar charge. This is a pricing ceiling,
    # not an assertion about an invoice or the provider's discounted price.
    rate = max(Decimal(model["prices"][p]) for p in ("input", "cache_read", "cache_write"))
    cost = (inputs * rate + outputs * Decimal(model["prices"]["output"])) / Decimal(1000000)
    return {"launches": 0, "requests": 1, "tokens": inputs + outputs, "cost_usd": str(cost)}


def text_blocks(value, allowed):
    if isinstance(value, str):
        return
    if not isinstance(value, list):
        fail("only text and local function content is supported")
    for block in value:
        if not isinstance(block, dict) or block.get("type") not in allowed:
            fail("media, hosted content and unknown content blocks are unsupported")
        if block.get("type") == "tool_result":
            text_blocks(block.get("content", ""), {"text"})


def prepare(config, api, payload):
    if catalog_today() > CATALOG_VALID_UNTIL:
        fail("provider pricing/context catalog needs a new official-source review")
    if not isinstance(payload, dict):
        fail("request must be a JSON object")
    model = next((m for m in config["models"] if m["api"] == api and m["id"] == payload.get("model")), None)
    if model is None:
        fail("unpriced or unpinned model")
    payload = dict(payload)
    if api == "anthropic-messages":
        allowed = {"model", "max_tokens", "messages", "system", "tools", "tool_choice", "stream", "temperature",
                   "top_p", "top_k", "stop_sequences", "metadata", "thinking", "output_config", "service_tier"}
        if set(payload) - allowed or payload.get("service_tier", "standard_only") != "standard_only":
            fail("unsupported Anthropic options or service tier")
        payload["service_tier"] = "standard_only"
        text_blocks(payload.get("system", ""), {"text"})
        if not isinstance(payload.get("messages"), list):
            fail("messages must be explicit")
        for message in payload["messages"]:
            if not isinstance(message, dict) or message.get("role") not in {"user", "assistant", "system"}:
                fail("invalid message")
            text_blocks(message.get("content"), {"text", "tool_use", "tool_result", "thinking", "redacted_thinking"})
        for tool in payload.get("tools", []):
            if not isinstance(tool, dict) or tool.get("type", "custom") != "custom" or "input_schema" not in tool:
                fail("server tools are unsupported")
        output_key = "max_tokens"
    else:
        allowed = {"model", "input", "instructions", "tools", "tool_choice", "parallel_tool_calls", "stream",
                   "max_output_tokens", "temperature", "top_p", "text", "reasoning", "metadata", "store",
                   "prompt_cache_key", "prompt_cache_retention", "service_tier", "include", "truncation"}
        if set(payload) - allowed or payload.get("service_tier", "default") != "default":
            fail("unsupported Responses options or service tier")
        if payload.get("truncation", "disabled") != "disabled":
            fail("server-managed context is unsupported")
        if payload.get("prompt_cache_retention", "in-memory") != "in-memory":
            fail("extended cache retention is unsupported")
        reasoning = payload.get("reasoning", {})
        if (not isinstance(reasoning, dict) or set(reasoning) - {"effort", "summary"}
                or reasoning.get("effort", "medium") not in {"none", "minimal", "low", "medium", "high", "xhigh"}):
            fail("only standard bounded reasoning is supported")
        payload.update(store=False, service_tier="default", truncation="disabled")
        items = payload.get("input")
        if not isinstance(items, (list, str)):
            fail("input must be explicit")
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                fail("invalid input item")
            kind = item.get("type", "message")
            if kind == "message":
                text_blocks(item.get("content"), {"input_text", "output_text", "refusal"})
            elif kind not in {"function_call", "function_call_output", "reasoning"}:
                fail("hosted or media input is unsupported")
            elif kind == "function_call_output" and not isinstance(item.get("output"), str):
                fail("function outputs must be text")
        for tool in payload.get("tools", []):
            if not isinstance(tool, dict) or tool.get("type") != "function":
                fail("server tools are unsupported")
        output_key = "max_output_tokens"
    requested = payload.get(output_key, model["max_output_tokens"])
    if not positive(requested):
        fail("output maximum must be positive")
    payload[output_key] = min(requested, model["max_output_tokens"])
    return model, payload, amounts(model, model["context_window"], payload[output_key])


def usage_amounts(model, receipt):
    """None means incomplete, never zero. Anthropic input categories are disjoint."""
    try:
        counts = [receipt["input_tokens"], receipt["output_tokens"]]
        if model["api"] == "anthropic-messages":
            counts += [receipt["cache_read_input_tokens"], receipt["cache_creation_input_tokens"]]
        if any(type(n) is not int or n < 0 for n in counts):
            return None
        return amounts(model, counts[0] + sum(counts[2:]), counts[1])
    except (KeyError, TypeError):
        return None


class Receipt:
    def __init__(self, api):
        self.api, self.usage, self.complete, self.buffer = api, {}, False, b""

    def feed(self, data):
        self.buffer += data
        if len(self.buffer) > MAX_RESPONSE:
            raise ValueError("provider response too large")
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if not line.startswith(b"data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except ValueError:
                continue
            kind = event.get("type")
            if self.api == "anthropic-messages":
                if kind == "message_start":
                    self.usage.update(event.get("message", {}).get("usage", {}))
                elif kind == "message_delta":
                    self.usage.update(event.get("usage", {}))
                elif kind == "message_stop":
                    self.complete = True
            elif kind in {"response.completed", "response.incomplete"}:
                self.usage = event.get("response", {}).get("usage", {})
                self.complete = True


class Gateway(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, config, path, task, attempt, credentials, *, connection_factory=None):
        self.config, self.ledger_path, self.task, self.attempt = config, path, task, attempt
        self.credentials, self.token = credentials, secrets.token_urlsafe(32)
        self.connection_factory = connection_factory or (lambda host: http.client.HTTPSConnection(host, timeout=180))
        ledger.bind_provider_contract(path, contract_digest(config))
        super().__init__(("127.0.0.1", 0), Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass  # Never log prompts, credentials, headers or provider error bodies.

    def error(self, status, message):
        body = json.dumps({"error": {"type": "provider_budget_error", "message": message}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_POST(self):
        gateway = self.server
        operation = None
        sent = False
        connection = None
        try:
            token = self.headers.get("x-api-key") or self.headers.get("Authorization", "").removeprefix("Bearer ")
            if not hmac.compare_digest(token, gateway.token):
                self.error(401, "proxy credential required")
                return
            location = urlsplit(self.path)
            if location.query not in {"", "beta=true"}:
                fail("unsupported endpoint query")
            api = {"/openai/v1/responses": "openai-responses", "/anthropic/v1/messages": "anthropic-messages"}.get(location.path)
            if api is None:
                fail("endpoint is not admitted")
            if self.headers.get("Transfer-Encoding") or not self.headers.get("Content-Length", "").isdigit():
                fail("bounded Content-Length required")
            size = int(self.headers["Content-Length"])
            if size > MAX_BODY:
                fail("request body exceeds limit")
            payload = json.loads(self.rfile.read(size))
            model, payload, maximum = prepare(gateway.config, api, payload)
            # Client idempotency keys cannot suppress accounting. Every actual
            # HTTP attempt (including client retry) gets its own durable UUID.
            operation = "provider:" + str(uuid.uuid4())
            _, created = ledger.reserve(gateway.ledger_path, operation_id=operation, task_id=gateway.task,
                                        attempt_id=gateway.attempt, maximum=maximum, supported=ledger.DIMENSIONS,
                                        provider_contract=contract_digest(gateway.config))
            if not created:
                fail("request reservation replay")
            host, path, key_env = APIS[api]
            connection = gateway.connection_factory(host)
            headers = {"Content-Type": "application/json", "Accept-Encoding": "identity"}
            if api == "anthropic-messages":
                headers.update({"x-api-key": gateway.credentials[key_env], "anthropic-version": "2023-06-01"})
            else:
                headers["Authorization"] = "Bearer " + gateway.credentials[key_env]
            # http.client never retries or follows redirects. No client headers
            # (beta, location, tier, authorization) reach the provider.
            connection.request("POST", path, json.dumps(payload).encode(), headers)
            response = connection.getresponse()
            if response.status != 200:
                ledger.settle(gateway.ledger_path, operation, complete=False)
                self.error(502, "upstream response unavailable; reservation retained")
                return
            streaming = response.getheader("Content-Type", "").startswith("text/event-stream")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if streaming else "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            sent, self.close_connection = True, True
            receipt, chunks, total = Receipt(api), [], 0
            while True:
                data = response.read1(65536)
                if not data:
                    break
                total += len(data)
                if total > MAX_RESPONSE:
                    raise ValueError("response limit")
                if streaming:
                    receipt.feed(data)
                else:
                    chunks.append(data)
                self.wfile.write(data)
                self.wfile.flush()
            if not streaming:
                result = json.loads(b"".join(chunks))
                receipt.usage = result.get("usage")
                receipt.complete = (result.get("type") == "message" and result.get("stop_reason") is not None
                                    if api == "anthropic-messages" else result.get("status") in {"completed", "incomplete"})
            actual = usage_amounts(model, receipt.usage) if receipt.complete else None
            ledger.settle(gateway.ledger_path, operation, actual=actual, complete=actual is not None)
        except Exception as exc:
            if operation:
                try:
                    ledger.settle(gateway.ledger_path, operation, complete=False)
                except ledger.BudgetError:
                    pass
            if not sent:
                try:
                    self.error(429 if isinstance(exc, ledger.BudgetError) and exc.code == "AGGREGATE_BUDGET_EXHAUSTED" else 400,
                               exc.code if isinstance(exc, ledger.BudgetError) else "PROVIDER_BUDGET_REQUEST_FAILED")
                except OSError:
                    pass
        finally:
            if connection:
                connection.close()


def client_environment(config, directory, gateway, parent):
    """Isolated provider auth/config, proxy token only; gateway retains API keys."""
    env = {k: v for k, v in parent.items() if not re.search(
        r"TOKEN|SECRET|PASSWORD|API_KEY|CREDENTIAL|(?:^|_)AUTH(?:_|$)|PROXY|BASE_URL|ENDPOINT|GOOGLE_|AWS_|AZURE_|BEDROCK|VERTEX", k)
        and not k.startswith(("ANTHROPIC_", "OPENAI_", "CLAUDE_"))}
    base = f"http://127.0.0.1:{gateway.server_port}"
    env.update(PI_CODING_AGENT_DIR=str(directory / "pi"), CLAUDE_CONFIG_DIR=str(directory / "claude"),
               PICO_BUDGET_PROXY_TOKEN=gateway.token, ANTHROPIC_API_KEY=gateway.token,
               OPENAI_API_KEY=gateway.token, ANTHROPIC_BASE_URL=base + "/anthropic",
               OPENAI_BASE_URL=base + "/openai/v1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
               CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS="1")
    if not env.get("PI_ORCHESTRATOR_MODEL_CONFIG"):
        original_config = Path(parent.get("XDG_CONFIG_HOME", str(Path(parent.get("HOME", "~")) / ".config")))
        original_models = original_config / "pi-coding-orchestrator" / "models.yaml"
        if original_models.is_file():
            env["PI_ORCHESTRATOR_MODEL_CONFIG"] = str(original_models.resolve())
    for name, child in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                        ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state"), ("CODEX_HOME", "codex")):
        (directory / child).mkdir()
        env[name] = str(directory / child)
    # Claude worker allowlist retains ANTHROPIC_/CLAUDE_ variables, including
    # its isolated configuration directory and the gateway credential.
    (directory / "pi").mkdir()
    (directory / "claude").mkdir()
    providers = {}
    for model in config["models"]:
        api = model["api"]
        provider = providers.setdefault(model["provider"], {"api": api,
            "baseUrl": base + ("/anthropic" if api == "anthropic-messages" else "/openai/v1"),
            "apiKey": gateway.token, "models": []})
        provider["models"].append({"id": model["id"], "name": model["id"], "api": api, "input": ["text"],
                                  "reasoning": True, "contextWindow": model["context_window"],
                                  **({"compat": {"forceAdaptiveThinking": True}} if model["id"] == "claude-opus-5-5" else {}),
                                  "maxTokens": model["max_output_tokens"], "cost": {"input": float(model["prices"]["input"]),
                                  "output": float(model["prices"]["output"]), "cacheRead": float(model["prices"]["cache_read"]),
                                  "cacheWrite": float(model["prices"]["cache_write"])}})
    (directory / "pi" / "models.json").write_text(json.dumps({"providers": providers}))
    pin = json.loads(parent.get("PI_ORCHESTRATOR_CLAUDE_BACKEND_IDENTITY", "{}"))
    if pin.get("backend_executable"):
        # The bridge explicitly enables project Claude settings. Override that
        # CLI boundary, including nested SDK calls, so a repository cannot
        # replace the gateway env/key via .claude/settings.json or hooks.
        binary = str(Path(pin["backend_executable"]).resolve(strict=True))
        shim = directory / "claude-api-only"
        shim.write_text(f'''#!{sys.executable}
import json, os, sys
binary = {binary!r}
args = sys.argv[1:]
if '--version' not in args and '-v' not in args:
    cleaned, settings, i = [], {{'disableAllHooks': True}}, 0
    while i < len(args):
        arg = args[i]
        if arg == '--setting-sources':
            i += 2
            continue
        if arg.startswith('--setting-sources='):
            i += 1
            continue
        if arg == '--settings' or arg.startswith('--settings='):
            raw = args[i + 1] if arg == '--settings' else arg.split('=', 1)[1]
            try:
                value = json.loads(raw)
                if not isinstance(value, dict) or set(value) - {{'disableAllHooks', 'sandbox', 'permissions', 'autoMemoryEnabled', 'claudeMdExcludes'}}:
                    raise ValueError()
            except (ValueError, TypeError):
                sys.exit('PROVIDER_BUDGET_UNSUPPORTED: external or unsafe Claude settings')
            settings.update(value)
            i += 2 if arg == '--settings' else 1
            continue
        cleaned.append(arg)
        i += 1
    settings['disableAllHooks'] = True
    args = ['--setting-sources', '', '--strict-mcp-config', '--settings', json.dumps(settings), *cleaned]
os.execv(binary, [binary, *args])
''')
        shim.chmod(0o700)
        stat = shim.stat()
        pin = {**pin, "provider_budget_underlying_identity": dict(pin), "backend_executable": str(shim),
               "backend_device": stat.st_dev, "backend_inode": stat.st_ino}
        env["PI_ORCHESTRATOR_CLAUDE_BACKEND_IDENTITY"] = json.dumps(pin)
        (directory / "pi" / "claude-bridge.json").write_text(json.dumps({
            "provider": {"pathToClaudeCodeExecutable": str(shim)},
            "askClaude": {"enabled": True, "defaultIsolated": True}}))
    else:
        shim = directory / "claude-api-unavailable"
        shim.write_text("#!" + sys.executable + "\nraise SystemExit('PROVIDER_BUDGET_UNSUPPORTED: pinned Claude executable required')\n")
        shim.chmod(0o700)
        (directory / "pi" / "claude-bridge.json").write_text(json.dumps({
            "provider": {"pathToClaudeCodeExecutable": str(shim)}, "askClaude": {"enabled": True, "defaultIsolated": True}}))
    provider, model = config["lead_model"].split("/", 1)
    (directory / "pi" / "settings.json").write_text(json.dumps({"defaultProvider": provider, "defaultModel": model,
        "enabledModels": [m["provider"] + "/" + m["id"] for m in config["models"]],
        "extensions": [str(Path(__file__).resolve().parents[1] / "extensions/provider-budget-guard.ts"),
                       *config.get("extensions", [])], "cacheWarming": {"enabled": False}}))
    return env


def run(config_path, ledger_path, task, attempt, command):
    config = validate_launch(config_path, ledger_path)
    validate_project(Path.cwd())
    credentials = {APIS[m["api"]][2]: os.environ[APIS[m["api"]][2]] for m in config["models"]}
    gateway = Gateway(config, ledger_path, task, attempt, credentials)
    thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="pico-budget-") as tmp:
            env = client_environment(config, Path(tmp), gateway, os.environ)
            # Project extensions and credential commands cannot override the
            # gateway models. Explicit PICO -e arguments remain operational.
            help_result = subprocess.run([command[0], "--help"], env=env, capture_output=True, text=True, timeout=15)
            if help_result.returncode or any(flag not in help_result.stdout for flag in ("--no-extensions", "--no-approve")):
                fail("Pi runtime cannot isolate project/provider configuration")
            argv = [command[0], "--no-extensions", "--no-approve", "-e",
                    str(Path(__file__).resolve().parents[1] / "extensions" / "provider-budget-guard.ts")]
            if "--no-mcp" in help_result.stdout:
                argv += ["--no-mcp"]
            for extension in config.get("extensions", []):
                argv += ["-e", extension]
            argv += command[1:]
            child = subprocess.Popen(argv, env=env)
            def stop(signum, _frame):
                child.send_signal(signum)
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            return child.wait()
    finally:
        gateway.shutdown()
        gateway.server_close()


def rebind_main(argv):
    parser = argparse.ArgumentParser(description="Explicitly replace a provider contract only in a completely unused ledger")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--expected-contract", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--attempt-cost-usd", help="explicitly adjust only the unused attempt dollar ceiling; lifetime stays fixed")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        data = ledger.rebind_empty_provider_contract(args.ledger, expected=args.expected_contract,
            replacement=contract_digest(config), reason=args.reason, attempt_cost_usd=args.attempt_cost_usd)
        print(json.dumps({"ok": True, "provider_contract": data["provider_contract"],
                          "limits": data["limits"], "provider_contract_history": data["provider_contract_history"]}))
        return 0
    except ledger.BudgetError as exc:
        print(json.dumps({"ok": False, "error_code": exc.code, "error": exc.detail}), file=sys.stderr)
        return 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["rebind-empty"]:
        return rebind_main(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--task", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("Pi command required after --")
    try:
        return run(args.config, args.ledger, args.task, args.attempt, command)
    except ledger.BudgetError as exc:
        print(json.dumps({"ok": False, "error_code": exc.code, "error": exc.detail}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
