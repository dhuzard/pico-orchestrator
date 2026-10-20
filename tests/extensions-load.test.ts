/**
 * Every first-party Pi extension must import cleanly and export a default
 * extension factory that registers without throwing. A corrupted or unloadable
 * extension therefore fails `npm test` (and `npm run typecheck`) instead of
 * failing only when Pi starts.
 */
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { test } from "node:test";
import { DEFAULT_OBJECTIVES, WORKFLOW_COMMANDS, buildWorkflowPrompt } from "../extensions/workflow-commands.ts";
import workflowCommands from "../extensions/workflow-commands.ts";

const ROOT = join(import.meta.dirname, "..");
const EXTENSIONS_DIR = join(ROOT, "extensions");
/** Helper modules imported by extensions; never passed to `pi -e`. */
const LIBRARY_MODULES = new Set(["claude-backend-adapter.ts", "claude-worker.ts", "context-budget.ts"]);
const LAUNCHERS = ["scripts/use-latest.sh", "scripts/run-local.sh"];

const EXPECTED_REGISTRATIONS: Record<string, { tools: string[]; commands: string[]; events: string[] }> = {
  "model-router.ts": { tools: ["route_task"], commands: [], events: [] },
  "workflow-commands.ts": { tools: [], commands: [...WORKFLOW_COMMANDS], events: [] },
  "mission-controller.ts": { tools: [],
    commands: ["deliver", "deliver-status", "deliver-link", "deliver-pause", "deliver-resume", "deliver-cancel"], events: [] },
  "orchestration-lifecycle.ts": { tools: ["ClaudeWorker", "orchestration_record"], commands: [],
    events: ["before_agent_start", "tool_call", "tool_result", "turn_end",
      "session_compact", "session_compact_failed", "agent_before_settle", "session_start", "session_shutdown"] },
};

function extensionModules(): string[] {
  return readdirSync(EXTENSIONS_DIR).filter((name) => name.endsWith(".ts") && !LIBRARY_MODULES.has(name)).sort();
}

interface Recorder {
  tools: string[];
  commands: Map<string, { handler: (args: unknown, ctx: unknown) => Promise<void> }>;
  events: string[];
  sent: string[];
}

function fakePi(sendUserMessage?: (text: string) => void): { pi: any; recorded: Recorder } {
  const recorded: Recorder = { tools: [], commands: new Map(), events: [], sent: [] };
  const pi = {
    on(name: string) { recorded.events.push(name); },
    registerTool(tool: { name: string; execute: unknown }) {
      assert.equal(typeof tool.execute, "function", `${tool.name} must define execute`);
      recorded.tools.push(tool.name);
    },
    registerCommand(name: string, options: { handler: (args: unknown, ctx: unknown) => Promise<void> }) {
      assert.equal(typeof options.handler, "function", `/${name} must define a handler`);
      assert.equal(recorded.commands.has(name), false, `/${name} registered twice`);
      recorded.commands.set(name, options);
    },
    sendUserMessage(text: string) { (sendUserMessage ?? ((value) => recorded.sent.push(value)))(text); },
    getActiveTools() { return []; },
  };
  return { pi, recorded };
}

function fakeContext() {
  const notices: { message: string; level: string }[] = [];
  return { notices, ctx: { cwd: ROOT, hasUI: false, signal: undefined,
    ui: { notify(message: string, level: string) { notices.push({ message, level }); }, setEditorText() {} } } };
}

test("every first-party extension exports a loadable default factory", async () => {
  const modules = extensionModules();
  assert.deepEqual(modules, Object.keys(EXPECTED_REGISTRATIONS).sort(),
    "new extensions must be added to EXPECTED_REGISTRATIONS (or LIBRARY_MODULES if not an extension)");
  for (const name of modules) {
    const module = await import(pathToFileURL(join(EXTENSIONS_DIR, name)).href);
    assert.equal(typeof module.default, "function", `${name} must export a default extension factory`);
    const { pi, recorded } = fakePi();
    assert.doesNotThrow(() => module.default(pi), `${name} factory threw during registration`);
    const expected = EXPECTED_REGISTRATIONS[name];
    assert.deepEqual([...recorded.tools].sort(), [...expected.tools].sort(), `${name} tools`);
    assert.deepEqual([...recorded.commands.keys()].sort(), [...expected.commands].sort(), `${name} commands`);
    for (const event of expected.events) assert.ok(recorded.events.includes(event), `${name} must handle ${event}`);
  }
});

test("library modules are imported by extensions and are not extension factories", async () => {
  for (const name of LIBRARY_MODULES) {
    const module = await import(pathToFileURL(join(EXTENSIONS_DIR, name)).href);
    assert.equal(module.default, undefined, `${name} must not look like an extension`);
    const importers = extensionModules().filter((ext) =>
      readFileSync(join(EXTENSIONS_DIR, ext), "utf8").includes(`./${name}`));
    assert.ok(importers.length > 0, `${name} is unused`);
  }
});

test("canonical launchers load exactly the first-party extension set", () => {
  for (const launcher of LAUNCHERS) {
    const text = readFileSync(join(ROOT, launcher), "utf8");
    const loaded = [...text.matchAll(/-e "\$(?:ORCH_REPO|RUNTIME)\/extensions\/([^"]+)"/g)].map((match) => match[1]).sort();
    assert.deepEqual(loaded, extensionModules(), `${launcher} must load every extension exactly once`);
    assert.match(text, /PI_ORCHESTRATOR_CLAUDE_BACKEND_IDENTITY/, `${launcher} must pin the Claude backend`);
  }
});

test("workflow commands never throw and use safe default objectives", async () => {
  for (const command of WORKFLOW_COMMANDS.filter((name) => name !== "initialize" && name !== "lookup")) {
    for (const args of ["", "   ", undefined]) {
      const { pi, recorded } = fakePi();
      workflowCommands(pi);
      const { ctx, notices } = fakeContext();
      await assert.doesNotReject(recorded.commands.get(command)!.handler(args, ctx), `/${command} ${String(args)}`);
      if (DEFAULT_OBJECTIVES[command]) {
        assert.equal(recorded.sent.length, 1, `/${command} must dispatch with a default objective`);
        assert.ok(recorded.sent[0].includes(DEFAULT_OBJECTIVES[command]));
        assert.ok(recorded.sent[0].includes("PI_ORCHESTRATION_RUN_V1"));
        assert.equal(recorded.sent[0].includes("$ARGUMENTS"), false);
        assert.deepEqual(notices, []);
      } else {
        assert.equal(recorded.sent.length, 0, `/${command} requires an objective`);
        assert.deepEqual(notices, [{ message: `Usage: /${command} <objective>`, level: "error" }]);
      }
    }
  }
  for (const required of ["audit", "audit-backend", "audit-frontend", "quick-audit", "update-agents"]) {
    assert.ok(DEFAULT_OBJECTIVES[required], `/${required} must have a safe default objective`);
  }
});

test("lookup answers directly without starting orchestration", async () => {
  const { pi, recorded } = fakePi();
  workflowCommands(pi);
  const { ctx, notices } = fakeContext();
  await recorded.commands.get("lookup")!.handler("Where is the guide link?", ctx);
  assert.equal(notices.length, 0);
  assert.equal(recorded.sent.length, 1);
  assert.match(recorded.sent[0], /Where is the guide link\?/);
  assert.doesNotMatch(recorded.sent[0], /PI_ORCHESTRATION_RUN_V1/);
  assert.match(recorded.sent[0], /Do not edit files/);
});

test("workflow prompt loads a compact core and points to detailed protocol", () => {
  const built = buildWorkflowPrompt("orchestrate", "Inspect the repository");
  assert.ok(built.ok);
  if (!built.ok) return;
  const full = readFileSync(join(ROOT, "prompts", "orchestrate.md"), "utf8");
  assert.ok(built.prompt.length < full.length / 2);
  assert.match(built.prompt, /prompts\/orchestrate\.md/);
  assert.match(built.prompt, /route_task/);
  assert.match(built.prompt, /## Completion/);
});

test("workflow command failures are reported, not thrown into Pi", async () => {
  const { pi, recorded } = fakePi(() => { throw new Error("queue closed"); });
  workflowCommands(pi);
  const { ctx, notices } = fakeContext();
  await assert.doesNotReject(recorded.commands.get("audit")!.handler("", ctx));
  assert.deepEqual(notices, [{ message: "/audit failed: queue closed", level: "error" }]);

  const missing = buildWorkflowPrompt("audit", "", join(ROOT, "does-not-exist", "orchestrate.md"));
  assert.equal(missing.ok, false);
  assert.equal(missing.ok === false && missing.reason, "prompt_unavailable");
});

test("workflow objectives are inserted literally", () => {
  const objective = "Fix $& and $' and $` and $1 handling";
  const built = buildWorkflowPrompt("orchestrate", objective);
  assert.ok(built.ok);
  assert.ok(built.ok && built.prompt.includes(objective));
});
