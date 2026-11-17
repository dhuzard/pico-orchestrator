import assert from "node:assert/strict";
import { test } from "node:test";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import type { JsonObject, Model, Tool } from "@earendil-works/pi-ai";
import { stream } from "@earendil-works/pi-ai/api/anthropic-messages";
import { validateToolArguments } from "@earendil-works/pi-ai/utils/validation";
import { normalizeContext } from "@earendil-works/pi-ai/utils/transcript";
import { Value } from "typebox/value";
import { createOrchestrationLifecycle } from "../extensions/orchestration-lifecycle.ts";

function recordTool(): Tool {
  const tools: Tool[] = [];
  createOrchestrationLifecycle()({ on() {}, registerTool(tool: Tool) { tools.push(tool); } } as unknown as ExtensionAPI);
  return tools.find((tool) => tool.name === "orchestration_record")!;
}

const completedPhase = { action: "event", event: { kind: "phase", phase: "one-line", status: "completed" } };

test("native Anthropic adapter exposes recorder actions and nested events before transport", async (t) => {
  const tool = recordTool();
  const captured: Record<string, any>[] = [];
  const fetch = t.mock.method(globalThis, "fetch", async () => { throw new Error("network forbidden in schema test"); });
  const model: Model<"anthropic-messages"> = {
    id: "claude-opus-5-5", name: "offline fixture", api: "anthropic-messages", provider: "anthropic",
    baseUrl: "http://127.0.0.1:1", reasoning: true, input: ["text"],
    cost: { input: 4, output: 20, cacheRead: 0.2, cacheWrite: 8 }, contextWindow: 1_000_000, maxTokens: 128_000,
    compat: { forceAdaptiveThinking: true },
  };
  const result = await stream(model, normalizeContext({
    messages: [
      { role: "system", content: "offline schema fixture", toolsAdded: [tool], timestamp: 0 },
      { role: "user", content: "Complete the phase", timestamp: 1 },
    ],
  }), {
    apiKey: "sk-ant-api03-fake-offline", maxTokens: 16, maxRetries: 0,
    onPayload(payload) { captured.push(payload as Record<string, any>); throw new Error("OFFLINE_SCHEMA_CAPTURE"); },
  }).result();
  assert.equal(result.errorMessage, "OFFLINE_SCHEMA_CAPTURE");
  assert.equal(fetch.mock.callCount(), 0);
  assert.equal(captured.length, 1);
  const schema = captured[0].tools.find((item: any) => item.name === tool.name).input_schema;
  assert.equal(schema.type, "object");
  assert.deepEqual(schema.required, ["action"]);
  assert.ok(schema.properties.action);
  assert.ok(schema.properties.event);
  assert.ok(schema.properties.integrate_wave_id);
  assert.equal(Value.Check(schema, completedPhase), true);
  assert.equal(Value.Check(schema, { ...completedPhase, event: JSON.stringify(completedPhase.event) }), false);
  assert.equal(Value.Check(schema, { action: "invented" }), false);
  // Pi drops the root union on the wire. Action-dependent requirements still
  // belong to the unchanged local validator, with the description guiding callers.
  assert.equal(Value.Check(schema, { action: "event" }), true);
  assert.equal(Value.Check(tool.parameters, { action: "event" }), false);
  assert.match(tool.description, /event needs a nested event object, never a JSON string/);
});

test("recorder preserves every action branch and rejects malformed calls before execution", () => {
  const tool = recordTool();
  const schema = tool.parameters;
  assert.ok("anyOf" in schema);
  const originalUnion = { anyOf: schema.anyOf };
  const valid: JsonObject[] = [
    { action: "status" },
    { action: "bind_workspace", task_id: "one-line", step_id: "claude_worker", path: "/tmp/worker" },
    { action: "declare_wave", wave_id: "wave-1", tasks: [{ task_id: "one-line", write_scope: ["settings.json"] }] },
    { action: "integrate_begin", wave_id: "wave-1", path: "/tmp/integration" },
    { action: "integrate_end", wave_id: "wave-1" },
    { action: "integrate_abort", wave_id: "wave-1" },
    completedPhase,
    { action: "finish", status: "COMPLETED", integrate_wave_id: "wave-1" },
    { action: "finish", status: "PARTIAL", blocker_code: "TEST_BLOCKER" },
  ];
  const invalid: JsonObject[] = [
    { action: "event", event: JSON.stringify(completedPhase.event) },
    { action: "event", ...completedPhase.event },
    { action: "event" },
    { action: "event", event: { ...completedPhase.event, status: "started" } },
    { action: "event", event: { ...completedPhase.event, only_if_open: true } },
    { action: "status", event: completedPhase.event },
    { action: "bind_workspace", step_id: "claude_worker" },
    { action: "declare_wave", wave_id: "wave-1", tasks: [] },
    { action: "integrate_end" },
    { action: "finish", status: "PARTIAL", integrate_wave_id: "wave-1" },
    { action: "finish", status: "COMPLETED", integrate_wave_id: "" },
    { action: "finish", status: "in_progress" },
    { action: "invented" },
  ];
  for (const args of valid) {
    assert.equal(Value.Check(originalUnion, args), true);
    assert.equal(Value.Check(schema, args), true);
    assert.deepEqual(validateToolArguments(tool, { type: "toolCall", id: "fixture", name: tool.name, arguments: args }), args);
  }
  for (const args of invalid) {
    assert.equal(Value.Check(originalUnion, args), false);
    assert.equal(Value.Check(schema, args), false);
    assert.throws(() => validateToolArguments(tool, { type: "toolCall", id: "fixture", name: tool.name, arguments: args }), /Validation failed/);
  }
});
