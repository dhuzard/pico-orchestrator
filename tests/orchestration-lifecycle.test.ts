import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Value } from "typebox/value";
import { createOrchestrationLifecycle, runJson, scopesOverlap } from "../extensions/orchestration-lifecycle.ts";
import { classifyClaudeInvocationError, resolveClaudeBackendModel } from "../extensions/claude-backend-adapter.ts";
import { buildWorkerEnv, claudeWorkerArgs, describeDenials, inGitDirectory, parseClaudeResult, WORKER_ENV_CREDENTIALS, WORKER_ENV_NAMES,
  WORKER_SANDBOX_SETTINGS, WorkerConfigError, workerSandboxEnabled } from "../extensions/claude-worker.ts";

type Handler = (event: any, context?: any) => Promise<any> | any;

function harness(preflight: Record<string, unknown>, failEvent = false, verifyOk = true,
                 options: { scopeOk?: boolean; worktreeOk?: boolean; worktreeBlocker?: string; entries?: any[];
                   parentStatus?: string; sessionId?: string; settleFails?: boolean; completionDetail?: string;
                   completionError?: string; limits?: Record<string, unknown>; executable?: string;
                   routeConflict?: boolean; reroutePhases?: string[];
                   escalationWorkerConflict?: boolean } = {}) {
  const handlers = new Map<string, Handler>();
  const tools = new Map<string, any>();
  const calls: { script: string; args: string[]; input?: Record<string, unknown> }[] = [];
  // Parent checkout state as seen by the fake snapshot operation.
  const checkout = { digest: "clean", status: [] as string[] };
  let escalated = false;
  const command = async (script: string, args: string[], input?: Record<string, unknown>) => {
    calls.push({ script, args, input });
    if (script.endsWith("session_scope.py")) {
      if (input?.operation === "snapshot") return { ok: true, digest: checkout.digest, status: [...checkout.status] };
      if (input?.operation === "worktree") return options.worktreeOk === false
        ? { ok: false, blocker_code: options.worktreeBlocker ?? "WORKER_WORKSPACE_INVALID" }
        : { ok: true, blocker_code: null, path: input.path };
      return options.scopeOk === false
        ? { ok: false, blocker_code: "SESSION_REPOSITORY_MISMATCH" } : { ok: true, blocker_code: null };
    }
    if (script.endsWith("autonomy_policy.py")) return options.limits ?? { ok: true, parallel_tasks: 3, open_prs: 2, pr_granularity: "batch" };
    if (script.endsWith("context_budget.py")) {
      if (args[0] === "policy") return { ok: true, policy: { schema_version: 1, checkpoint: { max_output_tokens: 4096 } } };
      if (args[0] === "checkpoint") return { ok: true, reference: "checkpoints/o__r/parent-test/0001.json",
        checkpoint: { schema_version: 1, run_id: "parent-test", sequence: 1, ...input } };
      return { ok: true };
    }
    if (script.endsWith("backend_preflight.py")) return input?.verify_backend ? { ok: verifyOk, blocker_code: verifyOk ? null : "MODEL_BACKEND_MISMATCH" }
      : preflight.ok ? { checked_steps: [{ step_id: "claude_worker", backend: "claude", model: "anthropic/claude-opus-5-5",
          backend_model: "claude-opus-5-5", backend_executable: options.executable ?? "/fake/claude",
          backend_version_output: "2.1.287 (Claude Code)", backend_device: 1, backend_inode: 2 }], ...preflight }
      : preflight;
    if (args[0] === "start") return { run_id: "parent-test", status: "IN_PROGRESS" };
    if (args[0] === "status") return { run_id: args[1], status: options.parentStatus ?? "IN_PROGRESS",
      reroute_phases: options.reroutePhases ?? [] };
    if (args[0] === "settle" && options.settleFails) return { ok: false, error_code: "PARENT_STORAGE_UNAVAILABLE" };
    if (args[0] === "event" && failEvent) return { ok: false };
    if (args[0] === "event" && options.routeConflict && !escalated && input?.kind === "phase" && input.route === "CLAUDE_CODEX")
      return { ok: false, error_code: "PARENT_EVENT_CONFLICT" };
    if (args[0] === "event" && options.escalationWorkerConflict && input?.kind === "escalation")
      return { ok: false, error_code: "PARENT_ESCALATION_WORKERS_IN_FLIGHT" };
    if (args[0] === "event" && input?.kind === "escalation") escalated = true;
    if (args[0] === "finish" && args[2] === "COMPLETED" && options.completionError) {
      return { ok: false, error_code: options.completionError, lifecycle_state: "IN_PROGRESS",
        error: options.completionDetail ?? null };
    }
    if (args[0] === "finish") return { ok: true, trace_path: "/tmp/state/runs/parent-test.json" };
    return { ok: true };
  };
  const defaultContext = { cwd: "/tmp/target", ui: { notify() {} } };
  const entries = options.entries ?? [];
  const pi = {
    on(name: string, handler: Handler) { handlers.set(name, (event, context) => handler(event, context ?? defaultContext)); },
    registerTool(tool: any) { tools.set(tool.name, tool); },
    appendEntry(customType: string, data: unknown) { entries.push({ type: "custom", customType, data }); },
    getActiveTools() { return ["route_task", "spawn_subagent", "ClaudeWorker", "orchestration_record"]; },
  } as unknown as ExtensionAPI;
  createOrchestrationLifecycle(command)(pi);
  const parentCalls = (operation: string) =>
    calls.filter((call) => call.script.endsWith("parent_orchestration.py") && call.args[0] === operation);
  return { handlers, tools, calls, parentCalls, checkout, entries };
}

const CODEX_HIGH = "openai-codex/gpt-6.1-sol";
function routeFor(id: string, options: { workerMutation?: "allowed" | "forbidden"; specifierEffort?: string;
  reviewerEffort?: string; specifierTier?: string } = {}) {
  return {
    toolName: "route_task",
    toolCallId: `route-${id}`,
    details: { result: { ok: true, classification: { base: { id } },
      selected_route: "CODEX_CLAUDE_CODEX", policy_version: 7,
      execution_plan: { steps: [{ id: "codex_specifier", family: "codex", mutation_permission: "forbidden" },
        { id: "claude_worker", family: "claude", mutation_permission: options.workerMutation ?? "forbidden" },
        { id: "codex_reviewer", family: "codex", mutation_permission: "forbidden" }] },
      capacity_assignments: [
        { step_id: "codex_specifier", assigned_capacity_tier: options.specifierTier ?? "high",
          assigned_reasoning_effort: options.specifierEffort ?? "high" },
        { step_id: "claude_worker", assigned_capacity_tier: "high", assigned_reasoning_effort: "high" },
        { step_id: "codex_reviewer", assigned_capacity_tier: "high",
          assigned_reasoning_effort: options.reviewerEffort ?? "high" }],
      backend_bindings: {
        codex: { tool: "spawn_subagent", capacity_models: { high: { model: CODEX_HIGH },
          economy: { model: "openai-codex/gpt-6-luna" } } },
        claude: { tool: "ClaudeWorker", capacity_models: { high: { model: "anthropic/claude-opus-5-5" } } } } } },
    content: [{ type: "text", text: "route selected" }],
  };
}
const routed = routeFor("S1");

test("routed phase records scores and model-capacity reasons for the viewer", async () => {
  const h = harness({ ok: true, status: "READY" });
  const result = structuredClone(routeFor("scores")) as any;
  const route = result.details.result;
  route.classification.effective = { task_type: "migration", ambiguity: 8, failure_cost: "high",
    semantic_correctness: "critical", diagnosis_required: false, implementation_scope: "broad",
    security_sensitive: true, cross_layer: true,
    ambiguity_assessment: Object.fromEntries(["outcome", "contract", "design", "evidence", "authority"]
      .map((name) => [name, { level: name === "contract" ? 8 : 2 }])),
    work_profile: { search: 7, analysis: 8, writing: 3, implementation: 6, verification: 7, coordination: 4 } };
  route.matches = { effective: ["CODEX_CLAUDE_CODEX"] };
  route.fallback_applied = false;
  for (const step of route.execution_plan.steps) step.role = step.id === "codex_reviewer" ? "reviewer" : step.id === "codex_specifier" ? "specifier" : "worker";
  for (const item of route.capacity_assignments) {
    item.selected_model_profile = { ambiguity_capacity: item.step_id === "claude_worker" ? 9 : 8 };
    item.rule = null;
  }
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" },
    { cwd: "/tmp/target", ui: { notify() {} } });
  await h.handlers.get("tool_result")!(result);
  const phase = h.parentCalls("event").find((call) => call.input?.phase === "scores" && call.input?.routing);
  assert.ok(phase);
  assert.equal((phase.input?.routing as any).ambiguity, 8);
  assert.equal((phase.input?.routing as any).ambiguity_dimensions.contract, 8);
  assert.equal((phase.input?.routing as any).work_profile.analysis, 8);
  assert.equal((phase.input?.routing as any).assignments[1].ambiguity_capacity, 9);
  assert.deepEqual((phase.input?.routing as any).signals, ["security_sensitive", "cross_layer"]);
  assert.equal(JSON.stringify(phase.input?.routing).includes("unresolved_choices"), false);
});

test("unsupported Claude backend blocks worktree and settles one parent trace", async () => {
  const h = harness({ ok: false, blocker_code: "MODEL_BACKEND_VERSION_UNSUPPORTED",
    step_id: "claude_worker", model: "anthropic/claude-opus-5-5",
    installed_backend_version: "2.1.251", required_backend_version: ">=2.1.280" });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" },
    { cwd: "/tmp/target", ui: { notify() {} } });
  const result = await h.handlers.get("tool_result")!(routed);
  assert.match(result.content.at(-1).text, /BLOCKED/);
  const guard = await h.handlers.get("tool_call")!({ toolName: "bash", input: { command: "git worktree add ../temp branch" } });
  assert.equal(guard.block, true);
  await h.handlers.get("session_shutdown")!({ reason: "quit" });
  assert.equal(h.parentCalls("start").length, 1);
  assert.equal(h.parentCalls("settle").length, 1);
  assert.ok(h.calls.some((call) => call.input?.kind === "worker" && call.input.blocker_code === "MODEL_BACKEND_VERSION_UNSUPPORTED"));
});

test("failed parent provenance update blocks worktree before preflight", async () => {
  const h = harness({ ok: true, status: "READY" }, true);
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" },
    { cwd: "/tmp/target", ui: { notify() {} } });
  const result = await h.handlers.get("tool_result")!(routed);
  assert.match(result.content.at(-1).text, /provenance update failed/);
  assert.equal(h.calls.some((call) => call.script.endsWith("backend_preflight.py")), false);
  assert.equal((await h.handlers.get("tool_call")!({ toolName: "bash", input: { command: "git worktree add ../temp branch" } })).block, true);
});

test("supported preflight permits worktree and explicit finalization", async () => {
  const h = harness({ ok: true, status: "READY" });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" },
    { cwd: "/tmp/target", ui: { notify() {} } });
  await h.handlers.get("tool_result")!(routed);
  assert.equal(await h.handlers.get("tool_call")!({ toolName: "bash", input: { command: "git worktree add ../temp branch" } }), undefined);
  const tool = h.tools.get("orchestration_record");
  const result = await tool.execute("call", { action: "finish", status: "COMPLETED" });
  assert.equal(result.details.ok, true);
  assert.equal(result.details.run_id, "parent-test");
  assert.match(result.content[0].text, /parent-test/);
  assert.match(result.content[0].text, /Run stats:.*total tokens unknown/);
  assert.match(result.content[0].text, /Trace: \/tmp\/state\/runs\/parent-test\.json/);
  // The render command uses this runtime's absolute path, never a target-relative tools/ path.
  assert.match(result.content[0].text, /Render it with: python3 \/\S+\/tools\/backlog\.py trace parent-test/);
  const afterFinish = await tool.execute("status", { action: "status" });
  assert.equal(afterFinish.details.run_id, "parent-test");
  assert.equal(afterFinish.details.active, false);
  await h.handlers.get("agent_before_settle")!({ outcome: "completed" });
  assert.equal(h.calls.filter((call) => call.args[0] === "finish").length, 1);
  assert.equal(h.calls.filter((call) => call.args[0] === "settle").length, 0);
});

test("assistant usage and successful subagent spawns are observed by the lifecycle", async () => {
  const h = harness({ ok: true, status: "READY" });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" });
  await h.handlers.get("turn_start")!({ turnIndex: 0 });
  await h.handlers.get("message_start")!({ message: { role: "assistant", timestamp: 123 } });
  await h.handlers.get("message_end")!({ message: { role: "assistant", provider: "openai-codex",
    model: "gpt-6.1-sol", timestamp: 123,
    usage: { input: 100, output: 20, cacheRead: 30, totalTokens: 150 } } });
  await h.handlers.get("tool_result")!({ toolName: "spawn_subagent", toolCallId: "worker-1",
    details: { subagent_id: "sub-1" }, content: [], isError: false });
  assert.ok(h.parentCalls("event").some((call) => call.input?.kind === "lead_usage"
    && call.input.total_tokens === 150 && call.input.model === "openai-codex/gpt-6.1-sol"
    && call.input.phase === "routing" && call.input.task_type === null
    && call.input.turn_index === 0 && typeof call.input.duration_ms === "number"));
  assert.equal(h.parentCalls("event").filter((call) => call.input?.kind === "spawn").length, 1);
});

test("rejected completion names its code and the run can still finish honestly", async () => {
  const h = harness({ ok: true, status: "READY" }, false, true, { completionError: "PARENT_REVIEW_MISSING" });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" },
    { cwd: "/tmp/target", ui: { notify() {} } });
  const tool = h.tools.get("orchestration_record");
  const rejected = await tool.execute("finish", { action: "finish", status: "COMPLETED" });
  assert.equal(rejected.details.ok, false);
  assert.equal(rejected.details.active, true);
  assert.match(rejected.content[0].text, /PARENT_REVIEW_MISSING/);
  const status = await tool.execute("status", { action: "status" });
  assert.equal(status.details.ok, true);
  const partial = await tool.execute("partial", { action: "finish", status: "PARTIAL" });
  assert.equal(partial.details.ok, true);
  assert.equal(partial.details.active, false);
});

test("route conflict remains blocked until an explicit escalation event", async () => {
  const h = harness({ ok: true, status: "READY" }, false, true, { routeConflict: true });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" });
  const tool = h.tools.get("orchestration_record");
  assert.equal((await tool.execute("initial", { action: "event", event: {
    kind: "phase", phase: "repair", status: "in_progress", route: "CODEX" } })).details.ok, true);
  assert.equal((await tool.execute("changed", { action: "event", event: {
    kind: "phase", phase: "repair", status: "in_progress", route: "CLAUDE_CODEX" } })).details.ok, false);
  assert.equal((await tool.execute("status", { action: "status" })).details.ok, false);
  assert.equal((await tool.execute("escalate", { action: "event", event: {
    kind: "escalation", phase: "repair", from_route: "CODEX", to_route: "CLAUDE_CODEX",
    reason: "REVIEW_FINDINGS" } })).details.ok, true);
  assert.equal((await tool.execute("status", { action: "status" })).details.ok, false);
  const reroute = routeFor("repair") as any;
  reroute.details.result.selected_route = "CLAUDE_CODEX";
  await h.handlers.get("tool_call")!({ toolName: "route_task", toolCallId: reroute.toolCallId,
    input: { classified_task: { id: "repair" } } });
  await h.handlers.get("tool_result")!(reroute);
  assert.equal((await tool.execute("status", { action: "status" })).details.ok, true);
});

test("recorder before /orchestrate refuses writes and names the start command", async () => {
  const h = harness({ ok: true, status: "READY" });
  const result = await h.tools.get("orchestration_record").execute("early", { action: "event",
    event: { kind: "phase", phase: "inspection", status: "in_progress" } });
  assert.equal(result.details.ok, false);
  assert.match(result.content[0].text, /\/orchestrate <objective>/);
  assert.equal(h.parentCalls("event").length, 0);
});

test("explicit escalation revokes the task's old worker approval", async () => {
  const h = harness({ ok: true, status: "READY" });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" });
  const route = routeFor("repair");
  await h.handlers.get("tool_call")!({ toolName: "route_task", toolCallId: route.toolCallId,
    input: { classified_task: { id: "repair" } } });
  await h.handlers.get("tool_result")!(route);
  const escalation = await h.tools.get("orchestration_record").execute("escalate", { action: "event", event: {
    kind: "escalation", phase: "repair", from_route: "CODEX", to_route: "CLAUDE_CODEX",
    reason: "REVIEW_FINDINGS" } });
  assert.equal(escalation.details.ok, true);
  const worker = await h.handlers.get("tool_call")!({ toolName: "spawn_subagent", toolCallId: "old",
    input: { label: "repair:codex_specifier", prompt: "read only" } });
  assert.match(worker.reason, /PARENT_ROUTE_RECHECK_REQUIRED/);
});

test("escalation waits for an active worker of the same task", async () => {
  const h = harness({ ok: true, status: "READY" });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" });
  await h.handlers.get("tool_result")!(routeFor("repair"));
  const call = { toolName: "spawn_subagent", toolCallId: "running",
    input: { label: "repair:codex_specifier", prompt: "read only" } };
  assert.equal(await h.handlers.get("tool_call")!(call), undefined);
  const before = h.parentCalls("event").length;
  const blocked = await h.tools.get("orchestration_record").execute("escalate", { action: "event", event: {
    kind: "escalation", phase: "repair", from_route: "CODEX", to_route: "CLAUDE_CODEX",
    reason: "REVIEW_FINDINGS" } });
  assert.equal(blocked.details.ok, false);
  assert.match(blocked.content[0].text, /PARENT_ESCALATION_WORKERS_IN_FLIGHT/);
  assert.equal(h.parentCalls("event").length, before);
});

test("a worker-in-flight escalation rejection cannot be cleared by status", async () => {
  const h = harness({ ok: true, status: "READY" }, false, true, { escalationWorkerConflict: true });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" });
  const tool = h.tools.get("orchestration_record");
  const rejected = await tool.execute("escalate", { action: "event", event: {
    kind: "escalation", phase: "repair", from_route: "CLAUDE_CODEX",
    to_route: "CODEX_CLAUDE_CODEX", reason: "REVIEW_FINDINGS" } });
  assert.equal(rejected.details.ok, false);
  assert.equal((await tool.execute("status", { action: "status" })).details.ok, false);
});

test("parent survives a turn, compaction, reload, and process restart in the same session", async () => {
  const entries: any[] = [];
  const context = { cwd: process.cwd(), ui: { notify() {} },
    sessionManager: { getSessionId: () => "session-a", getBranch: () => entries } };
  const first = harness({ ok: true, status: "READY" }, false, true, { entries });
  await first.handlers.get("session_start")!({ reason: "startup" }, context);
  await first.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, context);
  const tool = first.tools.get("orchestration_record");
  await tool.execute("inspection", { action: "event", event: {
    kind: "phase", phase: "inspection", status: "in_progress" } });
  await first.handlers.get("agent_before_settle")!({ outcome: "completed" });
  await first.handlers.get("session_compact")!({ reason: "manual", fromExtension: true });
  await first.handlers.get("session_shutdown")!({ reason: "reload" });
  assert.equal(first.parentCalls("settle").length, 0);

  const restarted = harness({ ok: true, status: "READY" }, false, true, { entries });
  await restarted.handlers.get("session_start")!({ reason: "startup" }, context);
  const resumed = await restarted.tools.get("orchestration_record").execute("status", { action: "status" });
  assert.equal(resumed.details.run_id, "parent-test");
  assert.equal(resumed.details.active, true);
  const oldWorker = await restarted.handlers.get("tool_call")!({ toolName: "message_subagent",
    toolCallId: "old-worker", input: { subagent_id: "worker-from-previous-process" } });
  assert.match(oldWorker.reason, /SESSION_SCOPE_VIOLATION/);
  await restarted.handlers.get("tool_result")!(routed);
  const worker = await restarted.tools.get("orchestration_record").execute("worker", { action: "event", event: {
    kind: "worker", phase: "S1", step_id: "codex_worker", status: "completed" } });
  assert.equal(worker.details.ok, true);
  const finished = await restarted.tools.get("orchestration_record").execute("finish", {
    action: "finish", status: "COMPLETED" });
  assert.equal(finished.details.run_id, "parent-test");
  assert.equal(restarted.parentCalls("start").length, 0);
  assert.equal(restarted.parentCalls("settle").length, 0);
  assert.equal(entries.at(-1).data.active, false);
});

test("restart with an unfinished routed phase requires route preflight again", async () => {
  const cwd = process.cwd();
  const entries: any[] = [{ type: "custom", customType: "pi-orchestration-parent-v1",
    data: { run_id: "parent-test", session_id: "session-a", target_cwd: cwd, active: true } }];
  const h = harness({ ok: true, status: "READY" }, false, true,
    { entries, reroutePhases: ["repair"] });
  await h.handlers.get("session_start")!({ reason: "startup" }, { cwd,
    sessionManager: { getSessionId: () => "session-a", getBranch: () => entries } });
  const status = await h.tools.get("orchestration_record").execute("status", { action: "status" });
  assert.equal(status.details.ok, false);
  assert.match(status.content[0].text, /rerun route_task/);
});

test("rerouting one restored phase does not clear another phase's recovery", async () => {
  const cwd = process.cwd();
  const entries: any[] = [{ type: "custom", customType: "pi-orchestration-parent-v1",
    data: { run_id: "parent-test", session_id: "session-a", target_cwd: cwd, active: true } }];
  const h = harness({ ok: true, status: "READY" }, false, true,
    { entries, reroutePhases: ["A", "B"] });
  await h.handlers.get("session_start")!({ reason: "startup" }, { cwd,
    sessionManager: { getSessionId: () => "session-a", getBranch: () => entries } });
  const route = routeFor("A");
  await h.handlers.get("tool_call")!({ toolName: "route_task", toolCallId: route.toolCallId,
    input: { classified_task: { id: "A" } } });
  await h.handlers.get("tool_result")!(route);
  const premature = await h.handlers.get("tool_call")!({ toolName: "spawn_subagent", toolCallId: "premature",
    input: { label: "A:codex_specifier", prompt: "read only" } });
  assert.match(premature.reason, /PARENT_ROUTE_RECHECK_REQUIRED/);
  const status = await h.tools.get("orchestration_record").execute("status", { action: "status" });
  assert.equal(status.details.ok, false);
  assert.match(status.content[0].text, /for B before/);
});

test("terminal and cross-session markers cannot restore a parent", async () => {
  for (const [sessionId, parentStatus] of [["session-a", "COMPLETED"], ["session-b", "IN_PROGRESS"]]) {
    const entries: any[] = [{ type: "custom", customType: "pi-orchestration-parent-v1",
      data: { run_id: "parent-test", session_id: "session-a", target_cwd: process.cwd(), active: true } }];
    const h = harness({ ok: true }, false, true, { entries, parentStatus, sessionId });
    await h.handlers.get("session_start")!({ reason: "startup" }, { cwd: process.cwd(),
      sessionManager: { getSessionId: () => sessionId, getBranch: () => entries } });
    const result = await h.tools.get("orchestration_record").execute("status", { action: "status" });
    assert.equal(result.details.active, false);
    assert.equal(h.parentCalls("event").length, 0);
  }
});

test("same-session continuation in another worktree blocks instead of creating a duplicate run", async () => {
  const entries: any[] = [{ type: "custom", customType: "pi-orchestration-parent-v1",
    data: { run_id: "parent-test", session_id: "session-a", target_cwd: process.cwd(), active: true } }];
  const h = harness({ ok: true }, false, true, { entries });
  const context = { cwd: "/tmp", ui: { notify() {} },
    sessionManager: { getSessionId: () => "session-a", getBranch: () => entries } };
  await h.handlers.get("session_start")!({ reason: "resume" }, context);
  const start = await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, context);
  assert.match(start.message.content, /SESSION_REPOSITORY_MISMATCH/);
  assert.equal(h.parentCalls("start").length, 0);
});

test("malformed active session marker fails closed", async () => {
  const entries: any[] = [{ type: "custom", customType: "pi-orchestration-parent-v1",
    data: { session_id: "session-a", active: true } }];
  const h = harness({ ok: true }, false, true, { entries });
  const context = { cwd: process.cwd(), ui: { notify() {} },
    sessionManager: { getSessionId: () => "session-a", getBranch: () => entries } };
  await h.handlers.get("session_start")!({ reason: "startup" }, context);
  const start = await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, context);
  assert.match(start.message.content, /PARENT_SESSION_MARKER_INVALID/);
  assert.equal(h.parentCalls("start").length, 0);
});

test("cancellation settles the active parent explicitly", async () => {
  const h = harness({ ok: true });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" });
  await h.handlers.get("agent_before_settle")!({ outcome: "aborted", continue: true });
  assert.equal(h.parentCalls("settle").length, 0);
  await h.handlers.get("agent_before_settle")!({ outcome: "aborted" },
    { cwd: "/tmp/target", hasPendingMessages: () => true });
  assert.equal(h.parentCalls("settle").length, 0);
  await h.handlers.get("agent_before_settle")!({ outcome: "aborted" });
  assert.equal(h.parentCalls("settle").length, 1);
});

test("an aborted Pi compaction does not settle the parent and a follow-up turn can finish it", async () => {
  const entries: any[] = [];
  const context = { cwd: process.cwd(), ui: { notify() {} },
    sessionManager: { getSessionId: () => "session-a", getBranch: () => entries } };
  const h = harness({ ok: true, status: "READY" }, false, true, { entries });
  await h.handlers.get("session_start")!({ reason: "startup" }, context);
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, context);
  await h.handlers.get("session_compact_failed")!({ reason: "threshold", error: new Error("This operation was aborted") });
  await h.handlers.get("agent_before_settle")!({ outcome: "aborted", continue: false });
  assert.equal(h.parentCalls("settle").length, 0);
  await h.handlers.get("session_shutdown")!({ reason: "reload" });
  const restarted = harness({ ok: true, status: "READY" }, false, true, { entries });
  await restarted.handlers.get("session_start")!({ reason: "startup" }, context);
  const status = await restarted.tools.get("orchestration_record").execute("status", { action: "status" });
  assert.equal(status.details.active, true);
  assert.equal(restarted.parentCalls("start").length, 0);
  const finish = await restarted.tools.get("orchestration_record").execute("finish", {
    action: "finish", status: "COMPLETED" });
  assert.equal(finish.details.ok, true);
});

test("failed settle retains the active identity and blocks a replacement run", async () => {
  const h = harness({ ok: true }, false, true, { settleFails: true });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" });
  await h.handlers.get("session_shutdown")!({ reason: "quit" });
  const replacement = await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" });
  assert.match(replacement.message.content, /BLOCKED/);
  assert.equal(h.parentCalls("start").length, 1);
});

test("hung parent subprocess returns a bounded failure", async () => {
  const dir = mkdtempSync(join(tmpdir(), "pico-parent-timeout-"));
  try {
    const script = join(dir, "hang.py");
    writeFileSync(script, "import time\ntime.sleep(5)\n");
    const started = Date.now();
    const result = await runJson(script, ["status"], undefined, 50);
    assert.equal(result.error_category, "Timeout");
    assert.ok(Date.now() - started < 2000);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("backend mismatch blocks before worktree and Claude invocation", async () => {
  const h = harness({ ok: true, status: "READY" }, false, false);
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" },
    { cwd: "/tmp/target", ui: { notify() {} } });
  await h.handlers.get("tool_result")!(routed);
  const worktree = await h.handlers.get("tool_call")!({ toolName: "bash", input: { command: "git worktree add ../temp branch" } });
  assert.equal(worktree.block, true);
  assert.match(worktree.reason, /MODEL_BACKEND_MISMATCH/);
  const worker = await h.handlers.get("tool_call")!({ toolName: "AskClaude", toolCallId: "mismatch",
    input: { model: "anthropic/claude-opus-5-5" } });
  assert.equal(worker.block, true);
});

test("rerouting one task replaces only its approval; another task keeps its own", async () => {
  const h = harness({ ok: true, status: "READY" });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" },
    { cwd: "/tmp/target", ui: { notify() {} } });
  await h.handlers.get("tool_result")!(routeFor("A"));
  // A single approval keeps the untagged, backward-compatible behavior.
  assert.equal(await h.handlers.get("tool_call")!({ toolName: "AskClaude", toolCallId: "ok",
    input: { model: "anthropic/claude-opus-5-5" } }), undefined);
  await h.handlers.get("tool_result")!({ toolName: "AskClaude", toolCallId: "ok", content: [], details: {} });
  await h.handlers.get("tool_result")!(routeFor("B"));
  const ask = (id: string, prompt: string) => ({ toolName: "AskClaude", toolCallId: id,
    input: { model: "anthropic/claude-opus-5-5", prompt } });
  const untagged = await h.handlers.get("tool_call")!(ask("u", "implement"));
  assert.match(untagged.reason, /WORKER_TASK_UNATTRIBUTED/);
  assert.equal(await h.handlers.get("tool_call")!(ask("a1", "[pico-task A:claude_worker] implement")), undefined);
  await h.handlers.get("tool_result")!({ toolName: "AskClaude", toolCallId: "a1", content: [], details: {} });
  // Re-routing B drops B's approval only.
  await h.handlers.get("tool_call")!({ toolName: "route_task", toolCallId: "reroute-b",
    input: { classified_task: { id: "B" } } });
  const stale = await h.handlers.get("tool_call")!(ask("b1", "[pico-task B:claude_worker] implement"));
  assert.match(stale.reason, /WORKER_TASK_UNROUTED/);
  assert.equal(await h.handlers.get("tool_call")!(ask("a2", "[pico-task A:claude_worker] implement")), undefined);
  const unknownStep = await h.handlers.get("tool_call")!(ask("a3", "[pico-task A:nope] implement"));
  assert.match(unknownStep.reason, /WORKER_STEP_UNKNOWN/);
  const wrongFamily = await h.handlers.get("tool_call")!(ask("a4", "[pico-task A:codex_reviewer] review"));
  assert.match(wrongFamily.reason, /MODEL_BACKEND_MISMATCH/);
});

test("spawn_subagent model and thinking are enforced from the routed step", async () => {
  const h = harness({ ok: true, status: "READY" });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" },
    { cwd: "/tmp/target", ui: { notify() {} } });
  await h.handlers.get("tool_result")!(routeFor("A", { specifierTier: "economy", specifierEffort: "low" }));
  const spawn = (id: string, input: Record<string, unknown>) => ({ toolName: "spawn_subagent", toolCallId: id, input });
  const ambiguous = await h.handlers.get("tool_call")!(spawn("s0", { task: "specify" }));
  assert.match(ambiguous.reason, /WORKER_STEP_UNATTRIBUTED/);
  const call = spawn("s1", { task: "specify", label: "A:codex_specifier" });
  assert.equal(await h.handlers.get("tool_call")!(call), undefined);
  assert.deepEqual([call.input.model, call.input.thinking], ["openai-codex/gpt-6-luna", "low"]);
  await h.handlers.get("tool_result")!({ toolName: "spawn_subagent", toolCallId: "s1", content: [],
    details: { subagent_id: "A-codex_specifier-1" } });
  const wrongModel = await h.handlers.get("tool_call")!(spawn("s2", { task: "review", label: "A:codex_reviewer",
    model: "openai-codex/gpt-6-luna" }));
  assert.match(wrongModel.reason, /MODEL_BACKEND_MISMATCH: .*model=openai-codex\/gpt-6\.1-sol/);
  const wrongThinking = await h.handlers.get("tool_call")!(spawn("s3", { task: "review", label: "A:codex_reviewer",
    thinking: "xhigh" }));
  assert.match(wrongThinking.reason, /REASONING_EFFORT_MISMATCH: .*thinking=high/);
  const reviewer = spawn("s4", { task: "review", label: "A:codex_reviewer", thinking: "high" });
  assert.equal(await h.handlers.get("tool_call")!(reviewer), undefined);
  assert.deepEqual([reviewer.input.model, reviewer.input.thinking], [CODEX_HIGH, "high"]);
});

test("a mutating Codex step needs a bound worktree named in its contract", async () => {
  const workspace = realpathSync(mkdtempSync(join(tmpdir(), "pico-codex-workspace-")));
  try {
    const h = harness({ ok: true, status: "READY" });
    await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" },
      { cwd: "/tmp/target", ui: { notify() {} } });
    const route = routeFor("A");
    (route.details.result.execution_plan.steps[0] as any).mutation_permission = "allowed";
    await h.handlers.get("tool_result")!(route);
    const spawn = (id: string, task: string) => ({ toolName: "spawn_subagent", toolCallId: id,
      input: { task, label: "A:codex_specifier" } });
    assert.match((await h.handlers.get("tool_call")!(spawn("c1", "implement"))).reason, /WORKER_WORKSPACE_UNBOUND/);
    await h.tools.get("orchestration_record").execute("b", { action: "bind_workspace", task_id: "A",
      step_id: "codex_specifier", path: workspace });
    assert.match((await h.handlers.get("tool_call")!(spawn("c2", "implement"))).reason, /WORKER_WORKSPACE_UNSTATED/);
    assert.equal(await h.handlers.get("tool_call")!(spawn("c3", `implement only inside ${workspace}`)), undefined);
  } finally { rmSync(workspace, { recursive: true, force: true }); }
});

test("wave declaration rejects overlapping scopes, unrouted tasks, and the parallel limit", async () => {
  assert.equal(scopesOverlap("src/a/**", "src/b/**"), false);
  assert.equal(scopesOverlap("src/*.ts", "src/x/y.py"), true);
  assert.equal(scopesOverlap("docs/a.md", "docs/a.md"), true);
  assert.equal(scopesOverlap("**", "anything"), true);
  assert.equal(scopesOverlap("./tests/test_a.py", "tests/test_b.py"), false);
  const h = harness({ ok: true, status: "READY" }, false, true, { limits: { ok: true, parallel_tasks: 2 } });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" },
    { cwd: "/tmp/target", ui: { notify() {} } });
  const record = h.tools.get("orchestration_record");
  const declare = (tasks: { task_id: string; write_scope: string[] }[]) =>
    record.execute("w", { action: "declare_wave", wave_id: "wave-1", tasks });
  assert.match((await declare([{ task_id: "A", write_scope: ["src/a/**"] }])).content[0].text, /WAVE_TASK_UNROUTED/);
  for (const id of ["A", "B", "C"]) await h.handlers.get("tool_result")!(routeFor(id));
  const overlap = await declare([{ task_id: "A", write_scope: ["src/**"] }, { task_id: "B", write_scope: ["src/b/x.ts"] }]);
  assert.equal(overlap.details.ok, false);
  assert.match(overlap.content[0].text, /WAVE_SCOPE_OVERLAP: A \(src\/\*\*\) and B \(src\/b\/x\.ts\)/);
  const unsafe = await declare([{ task_id: "A", write_scope: ["../outside"] }]);
  assert.match(unsafe.content[0].text, /WAVE_SCOPE_INVALID/);
  const limit = await declare([{ task_id: "A", write_scope: ["a"] }, { task_id: "B", write_scope: ["b"] },
    { task_id: "C", write_scope: ["c"] }]);
  assert.match(limit.content[0].text, /WAVE_LIMIT_EXCEEDED: .*limits\.parallel_tasks is 2/);
  const ok = await declare([{ task_id: "A", write_scope: ["src/a/**"] }, { task_id: "B", write_scope: ["src/b/**"] }]);
  assert.equal(ok.details.ok, true);
  assert.ok(h.calls.some((call) => call.input?.kind === "wave" && call.input.status === "declared"
    && JSON.stringify(call.input.task_ids) === JSON.stringify(["A", "B"])));
  const limitsCall = h.calls.find((call) => call.script.endsWith("autonomy_policy.py"));
  assert.deepEqual(limitsCall?.args, ["limits", "--worktree", "/tmp/target"]);
});

test("workers of different tasks overlap only inside a declared wave; integration is a barrier", async () => {
  const h = harness({ ok: true, status: "READY" });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" },
    { cwd: "/tmp/target", ui: { notify() {} } });
  for (const id of ["A", "B", "C"]) await h.handlers.get("tool_result")!(routeFor(id));
  const record = h.tools.get("orchestration_record");
  const spawn = (id: string, label: string) => ({ toolName: "spawn_subagent", toolCallId: id,
    input: { task: "work", label } });
  const result = (id: string) => h.handlers.get("tool_result")!({ toolName: "spawn_subagent", toolCallId: id,
    content: [], details: { subagent_id: `sub-${id}`, elapsedMs: 1000 } });
  assert.equal(await h.handlers.get("tool_call")!(spawn("a1", "A:codex_specifier")), undefined);
  const undeclared = await h.handlers.get("tool_call")!(spawn("b1", "B:codex_specifier"));
  assert.match(undeclared.reason, /WAVE_UNDECLARED/);
  const busy = await record.execute("w", { action: "declare_wave", wave_id: "w1",
    tasks: [{ task_id: "A", write_scope: ["a/**"] }, { task_id: "B", write_scope: ["b/**"] }] });
  assert.match(busy.content[0].text, /WAVE_WORKERS_IN_FLIGHT/);
  await result("a1");
  assert.equal((await record.execute("w", { action: "declare_wave", wave_id: "w1",
    tasks: [{ task_id: "A", write_scope: ["a/**"] }, { task_id: "B", write_scope: ["b/**"] }] })).details.ok, true);
  // One tool batch: both tasks' workers start before either returns.
  assert.equal(await h.handlers.get("tool_call")!(spawn("a2", "A:codex_specifier")), undefined);
  assert.equal(await h.handlers.get("tool_call")!(spawn("b2", "B:codex_specifier")), undefined);
  const outside = await h.handlers.get("tool_call")!(spawn("c2", "C:codex_specifier"));
  assert.match(outside.reason, /WAVE_UNDECLARED/);
  const early = await record.execute("i", { action: "integrate_begin", wave_id: "w1" });
  assert.equal(early.details.ok, false);
  assert.match(early.content[0].text, /INTEGRATION_WORKERS_IN_FLIGHT/);
  await result("a2");
  await result("b2");
  const concurrency = h.calls.filter((call) => call.input?.kind === "concurrency").map((call) => call.input);
  assert.equal(concurrency.at(-1)?.peak_workers, 2);
  assert.equal((await record.execute("i", { action: "integrate_begin", wave_id: "w1" })).details.ok, true);
  const during = await h.handlers.get("tool_call")!(spawn("a3", "A:codex_reviewer"));
  assert.match(during.reason, /INTEGRATION_IN_PROGRESS/);
  assert.match((await record.execute("d", { action: "declare_wave", wave_id: "w2",
    tasks: [{ task_id: "C", write_scope: ["c/**"] }] })).content[0].text, /INTEGRATION_IN_PROGRESS/);
  // The parent merges the wave's branches into its checkout.
  h.checkout.digest = "merged";
  h.checkout.status = [];
  const ended = await record.execute("e", { action: "integrate_end", wave_id: "w1" });
  assert.equal(ended.details.ok, true);
  assert.equal(ended.details.result.digest, "merged");
  assert.ok(h.calls.some((call) => call.input?.kind === "wave" && call.input.status === "integrated"));
  // Snapshot re-based: the parent's own merge does not latch a scope violation.
  assert.equal(await h.handlers.get("tool_call")!(spawn("a4", "A:codex_reviewer")), undefined);
  const review = await result("a4");
  assert.equal(review, undefined);
  assert.equal(await h.handlers.get("tool_call")!(spawn("a5", "A:codex_reviewer")), undefined);
  assert.equal(h.calls.some((call) => call.input?.blocker_code === "WORKER_SCOPE_VIOLATION"), false);
  assert.match((await record.execute("x", { action: "integrate_end", wave_id: "w1" })).content[0].text,
    /INTEGRATION_NOT_STARTED/);
});

/**
 * Fake Claude Code for ClaudeWorker tests: answers with a `--output-format json`
 * result whose text echoes its cwd, argv, and prompt. With FAKE_CLAUDE_BARRIER
 * set, each run waits until `count` runs have started, so sequential execution
 * times out. A prompt containing HANG never answers and records its pid.
 */
function fakeClaude(dir: string): string {
  const path = join(dir, "claude");
  writeFileSync(path, `#!/usr/bin/env node
const fs = require("node:fs"), path = require("node:path");
let prompt = "";
process.stdin.on("data", (part) => { prompt += part; });
process.stdin.on("end", () => {
  const reply = () => process.stdout.write(JSON.stringify({ type: "result", subtype: "success", is_error: false,
    result: JSON.stringify({ cwd: process.cwd(), args: process.argv.slice(2), prompt, env: process.env }),
    usage: { input_tokens: 120, output_tokens: 30, cache_read_input_tokens: 400 },
    total_cost_usd: 0.0125, duration_ms: 7, session_id: "fake-session",
    permission_denials: prompt.includes("DENY") ? [{ tool_name: "Write", tool_use_id: "t1",
      tool_input: { file_path: path.join(process.cwd(), "CHANGELOG.md"), content: "x" } }] : [] }));
  if (prompt.includes("HANG")) { fs.writeFileSync(path.join(${JSON.stringify(dir)}, "hang.pid"), String(process.pid)); setInterval(() => {}, 1000); return; }
  const barrier = process.env.FAKE_CLAUDE_BARRIER;
  if (!barrier) return reply();
  fs.writeFileSync(path.join(barrier, "started-" + process.pid), "");
  const deadline = Date.now() + 5000;
  const wait = () => fs.readdirSync(barrier).filter((name) => name.startsWith("started-")).length >= 2 ? reply()
    : Date.now() > deadline ? process.exit(3) : setTimeout(wait, 20);
  wait();
});
`, { mode: 0o755 });
  return path;
}

async function claudeWorkerRun(h: ReturnType<typeof harness>, id: string, input: Record<string, unknown>,
                               signal?: AbortSignal) {
  const guard = await h.handlers.get("tool_call")!({ toolName: "ClaudeWorker", toolCallId: id, input });
  if (guard) return { guard };
  const result = await h.tools.get("ClaudeWorker").execute(id, input, signal);
  const after = await h.handlers.get("tool_result")!({ toolName: "ClaudeWorker", toolCallId: id, input, ...result,
    isError: result.isError === true });
  return { result, after };
}

test("mutating Claude workers of a wave run concurrently, each in its own bound worktree", async () => {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "pico-claude-worker-")));
  const barrier = join(dir, "barrier");
  const workspaces = { A: join(dir, "wt-a"), B: join(dir, "wt-b") };
  for (const path of [barrier, ...Object.values(workspaces)]) mkdirSync(path);
  const previousCwd = process.cwd();
  process.env.FAKE_CLAUDE_BARRIER = barrier;
  // Workers receive only allowlisted variables; the test barrier is forwarded explicitly.
  process.env.PI_ORCHESTRATOR_WORKER_ENV_ALLOW = "FAKE_CLAUDE_BARRIER";
  try {
    const h = harness({ ok: true, status: "READY" }, false, true, { executable: fakeClaude(dir) });
    await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: dir, ui: { notify() {} } });
    for (const id of ["A", "B"]) await h.handlers.get("tool_result")!(routeFor(id, { workerMutation: "allowed" }));
    const record = h.tools.get("orchestration_record");
    for (const [task, path] of Object.entries(workspaces)) {
      await record.execute("b", { action: "bind_workspace", task_id: task, step_id: "claude_worker", path });
    }
    await record.execute("w", { action: "declare_wave", wave_id: "w1",
      tasks: [{ task_id: "A", write_scope: ["a/**"] }, { task_id: "B", write_scope: ["b/**"] }] });
    // Pi prepares a batch's calls in order, then executes them concurrently.
    const inputs = ["A", "B"].map((task) => ({ prompt: `[pico-task ${task}:claude_worker] implement ${task}` }));
    for (const [index, input] of inputs.entries()) {
      assert.equal(await h.handlers.get("tool_call")!({ toolName: "ClaudeWorker", toolCallId: `w${index}`, input }), undefined);
    }
    const results = await Promise.all(inputs.map((input, index) =>
      h.tools.get("ClaudeWorker").execute(`w${index}`, input, undefined)));
    for (const [index, result] of results.entries()) {
      await h.handlers.get("tool_result")!({ toolName: "ClaudeWorker", toolCallId: `w${index}`, input: inputs[index],
        ...result, isError: result.isError === true });
    }
    assert.equal(process.cwd(), previousCwd, "the Pi process directory never changes");
    for (const [index, task] of (["A", "B"] as const).entries()) {
      const { details, isError } = results[index];
      assert.equal(isError, false, results[index].content[0].text);
      const echoed = JSON.parse(results[index].content[0].text);
      assert.equal(echoed.cwd, workspaces[task]);
      assert.match(echoed.prompt, new RegExp(`^\\[pico-task ${task}:claude_worker\\]`));
      const flag = (name: string) => echoed.args[echoed.args.indexOf(name) + 1];
      assert.deepEqual([flag("--model"), flag("--effort"), flag("--permission-mode"), flag("--output-format")],
        ["claude-opus-5-5", "high", "acceptEdits", "json"]);
      assert.ok(echoed.args.includes("--no-session-persistence"));
      for (const denied of ["Bash(git push:*)", "Bash(gh:*)", "Bash(git worktree:*)"]) assert.ok(echoed.args.includes(denied), denied);
      assert.deepEqual([details.task_id, details.model, details.backend_model, details.reasoning_effort],
        [task, "anthropic/claude-opus-5-5", "claude-opus-5-5", "high"]);
    }
    assert.equal(h.calls.filter((call) => call.input?.kind === "concurrency").at(-1)?.input?.peak_workers, 2);
    const usage = h.calls.filter((call) => call.input?.kind === "usage").map((call) => call.input!);
    assert.equal(usage.length, 2);
    for (const entry of usage) {
      assert.deepEqual({ ...entry, duration_ms: undefined }, { kind: "usage", backend: "claude-code", duration_ms: undefined,
        input_tokens: 120, output_tokens: 30, cached_tokens: 400, total_tokens: null, estimated_cost_usd: 0.0125 });
      assert.equal(typeof entry.duration_ms, "number");
    }
    const launched = h.calls.filter((call) => call.input?.kind === "worker" && call.input.status === "in_progress");
    assert.deepEqual(launched.map((call) => call.input!.phase), ["A", "B"]);
    assert.deepEqual({ ...launched[0].input, phase: undefined }, { kind: "worker", phase: undefined, step_id: "claude_worker",
      status: "in_progress", model: "anthropic/claude-opus-5-5", backend: "claude-code", backend_model: "claude-opus-5-5",
      reasoning_effort: "high" });
  } finally {
    delete process.env.FAKE_CLAUDE_BARRIER;
    delete process.env.PI_ORCHESTRATOR_WORKER_ENV_ALLOW;
    rmSync(dir, { recursive: true, force: true });
  }
});

test("ClaudeWorker forces the routed model and effort and refuses unbound, untagged, or unrouted calls", async () => {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "pico-claude-guard-")));
  try {
    const h = harness({ ok: true, status: "READY" }, false, true, { executable: fakeClaude(dir) });
    const outside = await h.handlers.get("tool_call")!({ toolName: "ClaudeWorker", toolCallId: "o",
      input: { prompt: "[pico-task A:claude_worker] go" } }, { cwd: dir });
    assert.match(outside.reason, /WORKER_TASK_UNROUTED/);
    await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: dir, ui: { notify() {} } });
    await h.handlers.get("tool_result")!(routeFor("A", { workerMutation: "allowed" }));
    const run = (id: string, input: Record<string, unknown>) => claudeWorkerRun(h, id, input);
    assert.match((await run("u", { prompt: "implement" })).guard.reason, /WORKER_TASK_UNATTRIBUTED/);
    assert.match((await run("r", { prompt: "[pico-task Z:claude_worker] go" })).guard.reason, /WORKER_TASK_UNROUTED/);
    assert.match((await run("f", { prompt: "[pico-task A:codex_reviewer] go" })).guard.reason, /MODEL_BACKEND_MISMATCH/);
    assert.match((await run("m", { prompt: "[pico-task A:claude_worker] go", model: "sonnet" })).guard.reason,
      /MODEL_BACKEND_MISMATCH/);
    assert.match((await run("t", { prompt: "[pico-task A:claude_worker] go", thinking: "low" })).guard.reason,
      /REASONING_EFFORT_MISMATCH: .*thinking=high/);
    assert.match((await run("n", { prompt: "[pico-task A:claude_worker] go" })).guard.reason, /WORKER_WORKSPACE_UNBOUND/);
    // A mutating step never goes through the process-directory-bound AskClaude.
    const ask = await h.handlers.get("tool_call")!({ toolName: "AskClaude", toolCallId: "ask",
      input: { model: "anthropic/claude-opus-5-5", prompt: "[pico-task A:claude_worker] go" } });
    assert.match(ask.reason, /CLAUDE_WORKER_REQUIRED/);
    // Without a guard-resolved plan, execute refuses to start Claude Code.
    const direct = await h.tools.get("ClaudeWorker").execute("never-guarded", { prompt: "[pico-task A:claude_worker] go" });
    assert.equal(direct.isError, true);
    assert.match(direct.content[0].text, /WORKER_TASK_UNROUTED/);
    assert.equal(h.calls.some((call) => call.input?.kind === "usage"), false);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("a mutating Claude worker gets the full edit argv with git and GitHub operations still denied", () => {
  const cwd = "/repo/.pi/worktrees/s1";
  const args = claudeWorkerArgs({ executable: "claude", cwd, backendModel: "haiku", effort: "low", mode: "full" });
  const contract = args[args.indexOf("--append-system-prompt") + 1];
  assert.match(contract, /Your working directory \/repo\/\.pi\/worktrees\/s1 is the git worktree bound to your step: edit only inside it/);
  assert.deepEqual(args.filter((arg) => arg !== contract), ["-p", "--model", "haiku", "--effort", "low",
    "--permission-mode", "acceptEdits", "--tools", "Read,Edit,Write,Glob,Grep,Bash,Agent",
    "--allowedTools", "Read", "Edit", "Write", "Glob", "Grep", "Bash", "Agent",
    "--disallowedTools", "Bash(git push:*)", "Bash(gh:*)", "Bash(git worktree:*)", "Bash(git branch -d:*)",
    "Bash(git branch -D:*)", "Bash(git branch --delete:*)", "Bash(git -C:*)", "Bash(git -c:*)",
    "Bash(git --git-dir:*)", "Bash(git --work-tree:*)", "Bash(git --exec-path:*)", "Bash(git remote add:*)",
    "Bash(git remote set-url:*)", "Bash(env git:*)", "Bash(command git:*)", "Bash(exec git:*)",
    "Bash(env gh:*)", "Bash(command gh:*)", "Bash(exec gh:*)", "--append-system-prompt",
    "--output-format", "json", "--no-session-persistence"]);
  assert.ok(!args.includes("bypassPermissions") && !args.includes("--dangerously-skip-permissions"));
  assert.ok(!args.includes("--settings"), "the Claude Code sandbox is opt-in");
  const sandboxed = claudeWorkerArgs({ executable: "claude", cwd, backendModel: "haiku", effort: "low", mode: "full" }, true);
  assert.deepEqual(sandboxed.slice(-2), ["--settings", WORKER_SANDBOX_SETTINGS]);
  assert.deepEqual(JSON.parse(WORKER_SANDBOX_SETTINGS), { sandbox: { enabled: true, failIfUnavailable: true } });
});

test("a worker environment keeps only what Claude Code needs and never a GitHub or SSH credential", () => {
  const source = { PATH: "/bin", HOME: "/h", USER: "u", LANG: "C.UTF-8", LC_ALL: "C", TMPDIR: "/t",
    XDG_CONFIG_HOME: "/h/.config", ANTHROPIC_API_KEY: "a", CLAUDE_CODE_OAUTH_TOKEN: "c", NODE_EXTRA_CA_CERTS: "/ca.pem",
    https_proxy: "http://proxy:3128", NO_PROXY: "localhost",
    GH_TOKEN: "g1", GITHUB_TOKEN: "g2", GH_ENTERPRISE_TOKEN: "g3", GIT_ASKPASS: "/askpass", SSH_AUTH_SOCK: "/agent",
    NPM_TOKEN: "n", AWS_SECRET_ACCESS_KEY: "s", GIT_DIR: "/parent/.git", MY_SERVICE_API_KEY: "k",
    PI_ORCHESTRATOR_STATE_DIR: "/state" };
  const { env, dropped } = buildWorkerEnv(source);
  assert.deepEqual(Object.keys(env).sort(), ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "HOME", "LANG", "LC_ALL",
    "NODE_EXTRA_CA_CERTS", "NO_PROXY", "PATH", "TMPDIR", "USER", "XDG_CONFIG_HOME", "https_proxy"]);
  assert.equal(env.CLAUDE_CODE_OAUTH_TOKEN, "c");
  // Known credentials first, then everything else; each group sorted.
  assert.deepEqual(dropped, ["GH_ENTERPRISE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "GIT_ASKPASS", "NPM_TOKEN",
    "SSH_AUTH_SOCK", "AWS_SECRET_ACCESS_KEY", "GIT_DIR", "MY_SERVICE_API_KEY", "PI_ORCHESTRATOR_STATE_DIR"]);
  for (const name of WORKER_ENV_CREDENTIALS) assert.ok(!(name in buildWorkerEnv({ [name]: "x" }).env), name);
  // A prefix rule never forwards a listed credential name.
  assert.ok(!WORKER_ENV_CREDENTIALS.some((name) => WORKER_ENV_NAMES.includes(name)));

  // Extension: exact names, comma-separated, whitespace ignored; the user's explicit choice wins.
  const extended = buildWorkerEnv({ ...source, PI_ORCHESTRATOR_WORKER_ENV_ALLOW: " JAVA_HOME, MY_SERVICE_API_KEY ,,GH_TOKEN",
    JAVA_HOME: "/jdk" });
  assert.equal(extended.env.JAVA_HOME, "/jdk");
  assert.equal(extended.env.MY_SERVICE_API_KEY, "k");
  assert.equal(extended.env.GH_TOKEN, "g1");
  assert.ok(!("GITHUB_TOKEN" in extended.env));
  for (const bad of ["GH_*", "1ABC", "A-B", "A B", "$(id)"]) {
    assert.throws(() => buildWorkerEnv({ PI_ORCHESTRATOR_WORKER_ENV_ALLOW: bad }), WorkerConfigError, bad);
  }
  assert.equal(workerSandboxEnabled({}), false);
  assert.equal(workerSandboxEnabled({ PI_ORCHESTRATOR_WORKER_SANDBOX: "0" }), false);
  assert.equal(workerSandboxEnabled({ PI_ORCHESTRATOR_WORKER_SANDBOX: "1" }), true);
  for (const bad of ["yes", "true", "2"]) {
    assert.throws(() => workerSandboxEnabled({ PI_ORCHESTRATOR_WORKER_SANDBOX: bad }), WorkerConfigError, bad);
  }
});

test("ClaudeWorker withholds credentials from the worker process and records only their names", async () => {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "pico-claude-env-")));
  const workspace = join(dir, "wt");
  mkdirSync(workspace);
  const names = ["GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "PICO_TEST_EXTRA", "PI_ORCHESTRATOR_WORKER_ENV_ALLOW",
    "PI_ORCHESTRATOR_WORKER_SANDBOX", "ANTHROPIC_BASE_URL"];
  const previous = Object.fromEntries(names.map((name) => [name, process.env[name]]));
  Object.assign(process.env, { GH_TOKEN: "secret-gh", GITHUB_TOKEN: "secret-github", SSH_AUTH_SOCK: "/tmp/agent.sock",
    PICO_TEST_EXTRA: "extra", ANTHROPIC_BASE_URL: "http://127.0.0.1:9" });
  try {
    const h = harness({ ok: true, status: "READY" }, false, true, { executable: fakeClaude(dir) });
    await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: dir, ui: { notify() {} } });
    await h.handlers.get("tool_result")!(routeFor("A", { workerMutation: "allowed" }));
    await h.tools.get("orchestration_record").execute("b", { action: "bind_workspace", task_id: "A",
      step_id: "claude_worker", path: workspace });

    const plain = (await claudeWorkerRun(h, "e1", { prompt: "[pico-task A:claude_worker] go" })).result;
    assert.equal(plain.isError, false, plain.content[0].text);
    const echoed = JSON.parse(plain.content[0].text);
    for (const name of ["GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "PICO_TEST_EXTRA"]) assert.ok(!(name in echoed.env), name);
    assert.equal(echoed.env.PATH, process.env.PATH);
    assert.equal(echoed.env.ANTHROPIC_BASE_URL, "http://127.0.0.1:9");
    assert.ok(!echoed.args.includes("--settings"));
    // The record keeps at most 100 names (CI runners carry more): credentials
    // come first so they are always named; other names are in the full list.
    for (const name of ["GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK"]) {
      assert.ok(plain.details.env_dropped.includes(name), name);
    }
    assert.ok(buildWorkerEnv().dropped.includes("PICO_TEST_EXTRA"));
    assert.equal(plain.details.env_dropped_count, buildWorkerEnv().dropped.length);
    assert.ok(!JSON.stringify(plain.details).includes("secret-gh"), "values are never recorded");

    process.env.PI_ORCHESTRATOR_WORKER_ENV_ALLOW = "PICO_TEST_EXTRA";
    process.env.PI_ORCHESTRATOR_WORKER_SANDBOX = "1";
    const extended = (await claudeWorkerRun(h, "e2", { prompt: "[pico-task A:claude_worker] go" })).result;
    const echoedExtended = JSON.parse(extended.content[0].text);
    assert.equal(echoedExtended.env.PICO_TEST_EXTRA, "extra");
    assert.ok(!("GH_TOKEN" in echoedExtended.env));
    assert.equal(echoedExtended.args[echoedExtended.args.indexOf("--settings") + 1], WORKER_SANDBOX_SETTINGS);

    // An invalid allowlist fails closed before Claude Code starts.
    process.env.PI_ORCHESTRATOR_WORKER_ENV_ALLOW = "GH_*";
    const refused = (await claudeWorkerRun(h, "e3", { prompt: "[pico-task A:claude_worker] go" })).result;
    assert.equal(refused.isError, true);
    assert.equal(refused.details.error, "CONFIG_INVALID");
    assert.match(refused.content[0].text, /not started: PI_ORCHESTRATOR_WORKER_ENV_ALLOW entry "GH_\*" is not a variable name/);
    assert.match(refused.content[0].text, /Orchestration blocker: WORKER_CONFIG_INVALID/);
    // Claude Code never started, so no token counts are reported (the fake would report 120 input tokens).
    const usage = h.calls.filter((call) => call.input?.kind === "usage").at(-1)!.input!;
    assert.deepEqual([usage.input_tokens, usage.output_tokens, usage.estimated_cost_usd], [null, null, undefined]);
  } finally {
    for (const name of names) {
      if (previous[name] === undefined) delete process.env[name];
      else process.env[name] = previous[name];
    }
    rmSync(dir, { recursive: true, force: true });
  }
});

test("a worktree inside .git is never used for a mutating Claude worker", async () => {
  // Regression (run-2dc716699308): Claude Code protects every .git path, so a worker bound to
  // <repo>/.git/pico-worktrees/s1 had its Write denied even in acceptEdits mode.
  assert.equal(inGitDirectory("/r/.git/pico-worktrees/s1"), true);
  assert.equal(inGitDirectory("/r/sub/.git"), true);
  for (const path of ["/r/.pi/worktrees/s1", "/r/.claude/worktrees/s1", "/r/.github/x", "/r/a.git/s1"]) {
    assert.equal(inGitDirectory(path), false, path);
  }
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "pico-claude-gitdir-")));
  const workspace = join(dir, ".git", "pico-worktrees", "s1");
  mkdirSync(workspace, { recursive: true });
  try {
    const h = harness({ ok: true, status: "READY" }, false, true, { executable: fakeClaude(dir) });
    await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: dir, ui: { notify() {} } });
    await h.handlers.get("tool_result")!(routeFor("A", { workerMutation: "allowed" }));
    await h.tools.get("orchestration_record").execute("b", { action: "bind_workspace", task_id: "A",
      step_id: "claude_worker", path: workspace });
    const { guard } = await claudeWorkerRun(h, "g", { prompt: "[pico-task A:claude_worker] add CHANGELOG.md" });
    assert.match(guard.reason, /^WORKER_WORKSPACE_PROTECTED_PATH: .*\.git\/pico-worktrees\/s1/);
    assert.equal(h.calls.some((call) => call.input?.kind === "usage"), false, "Claude Code never starts");
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("bind_workspace reports a protected .git worktree path precisely", async () => {
  // The real session_scope verdict for such a path is pinned in tests/test_context_isolation.py.
  const h = harness({ ok: true, status: "READY" }, false, true,
    { worktreeOk: false, worktreeBlocker: "WORKER_WORKSPACE_PROTECTED_PATH" });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: "/tmp/target", ui: { notify() {} } });
  await h.handlers.get("tool_result")!(routeFor("A", { workerMutation: "allowed" }));
  const bound = await h.tools.get("orchestration_record").execute("b", { action: "bind_workspace", task_id: "A",
    step_id: "claude_worker", path: "/tmp/target/.git/pico-worktrees/s1" });
  assert.match(bound.content[0].text, /^BLOCKED: WORKER_WORKSPACE_PROTECTED_PATH: .*outside \.git/);
  const { guard } = await claudeWorkerRun(h, "g", { prompt: "[pico-task A:claude_worker] go" });
  assert.match(guard.reason, /WORKER_WORKSPACE_UNBOUND/);
});

test("ClaudeWorker names each denied tool call and path", async () => {
  assert.deepEqual(describeDenials([{ tool_name: "Write", tool_input: { file_path: "/w/CHANGELOG.md" } },
    { tool_name: "Bash", tool_input: { command: "git push" } }, { tool_name: "Agent" }, null]),
    ["Write /w/CHANGELOG.md", "Bash git push", "Agent", "unknown tool"]);
  assert.deepEqual(describeDenials(undefined), []);
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "pico-claude-deny-")));
  const workspace = join(dir, "wt");
  mkdirSync(workspace);
  try {
    const h = harness({ ok: true, status: "READY" }, false, true, { executable: fakeClaude(dir) });
    await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: dir, ui: { notify() {} } });
    await h.handlers.get("tool_result")!(routeFor("A", { workerMutation: "allowed" }));
    await h.tools.get("orchestration_record").execute("b", { action: "bind_workspace", task_id: "A",
      step_id: "claude_worker", path: workspace });
    const { result } = await claudeWorkerRun(h, "d", { prompt: "[pico-task A:claude_worker] DENY" });
    assert.match(result.content[0].text, new RegExp(`denied 1 tool call\\(s\\) outside the worker's permissions: Write ${workspace}/CHANGELOG\\.md`));
    assert.deepEqual(result.details.denied_calls, [`Write ${join(workspace, "CHANGELOG.md")}`]);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("a read-only ClaudeWorker step runs read tools in the target checkout", async () => {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "pico-claude-read-")));
  try {
    const h = harness({ ok: true, status: "READY" }, false, true, { executable: fakeClaude(dir) });
    await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: dir, ui: { notify() {} } });
    await h.handlers.get("tool_result")!(routeFor("A"));
    const { result } = await claudeWorkerRun(h, "r1", { prompt: "[pico-task A:claude_worker] review",
      model: "anthropic/claude-opus-5-5", thinking: "high" });
    const echoed = JSON.parse(result.content[0].text);
    assert.equal(echoed.cwd, dir);
    const flag = (name: string) => echoed.args[echoed.args.indexOf(name) + 1];
    assert.deepEqual([flag("--permission-mode"), flag("--tools")], ["default", "Read,Glob,Grep"]);
    assert.equal(result.details.mode, "read");
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("aborting a ClaudeWorker call kills its Claude Code process", async () => {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "pico-claude-abort-")));
  const workspace = join(dir, "wt");
  mkdirSync(workspace);
  try {
    const h = harness({ ok: true, status: "READY" }, false, true, { executable: fakeClaude(dir) });
    await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: dir, ui: { notify() {} } });
    await h.handlers.get("tool_result")!(routeFor("A", { workerMutation: "allowed" }));
    await h.tools.get("orchestration_record").execute("b", { action: "bind_workspace", task_id: "A",
      step_id: "claude_worker", path: workspace });
    const controller = new AbortController();
    const pending = claudeWorkerRun(h, "hang", { prompt: "[pico-task A:claude_worker] HANG" }, controller.signal);
    const pidFile = join(dir, "hang.pid");
    for (let tries = 0; tries < 250 && !existsSync(pidFile); tries += 1) await new Promise((done) => setTimeout(done, 20));
    const pid = Number(readFileSync(pidFile, "utf8"));
    controller.abort();
    const { result, after } = await pending;
    assert.equal(result.isError, true);
    assert.equal(result.details.error, "ABORTED");
    assert.match(result.content.at(-1).text, /Orchestration blocker: WORKER_ABORTED/);
    assert.equal(after, undefined);
    assert.throws(() => process.kill(pid, 0), "the Claude Code child is gone");
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("orchestration_record schema accepts early inspection and rejects invented status", () => {
  const h = harness({ ok: true, status: "READY" });
  const schema = h.tools.get("orchestration_record").parameters;
  assert.equal(Value.Check(schema, { action: "status" }), true);
  assert.equal(Value.Check(schema, { action: "event", event: {
    kind: "phase", phase: "inspection", status: "in_progress",
  } }), true);
  assert.equal(Value.Check(schema, { action: "event", event: {
    kind: "phase", phase: "inspection", status: "started",
  } }), false);
  assert.equal(Value.Check(schema, { action: "event", event: {
    kind: "phase", phase: "inspection",
  } }), false);
  assert.equal(Value.Check(schema, { action: "event", event: {
    kind: "child", phase: "phase-a", run_id: "run-child",
  } }), true);
});

test("canonical Claude model is translated only at AskClaude invocation", async () => {
  const h = harness({ ok: true, status: "READY" });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" },
    { cwd: "/tmp/target", ui: { notify() {} } });
  assert.deepEqual(resolveClaudeBackendModel("anthropic/claude-opus-5-5"),
    { ok: true, canonical_model: "anthropic/claude-opus-5-5", backend_model: "claude-opus-5-5" });
  assert.deepEqual(resolveClaudeBackendModel("sonnet"),
    { ok: true, canonical_model: "sonnet", backend_model: "sonnet" });
  assert.deepEqual(resolveClaudeBackendModel("haiku"),
    { ok: true, canonical_model: "haiku", backend_model: "haiku" });
  const call = { toolName: "AskClaude", toolCallId: "claude-call", input: {
    model: "anthropic/claude-opus-5-5", prompt: "fixture", mode: "read",
  } };
  await h.handlers.get("tool_result")!(routed);
  assert.equal(await h.handlers.get("tool_call")!(call), undefined);
  assert.equal(call.input.model, "claude-opus-5-5");
  const fakeClaudeArgs = ["claude", "--model", call.input.model, "-p", "fixture"];
  assert.deepEqual(fakeClaudeArgs.slice(0, 3), ["claude", "--model", "claude-opus-5-5"]);
  assert.equal(fakeClaudeArgs.includes("anthropic/claude-opus-5-5"), false);
  const success = await h.handlers.get("tool_result")!({ toolName: "AskClaude", toolCallId: "claude-call",
    content: [{ type: "text", text: "OK" }], details: {}, isError: false });
  assert.equal(success, undefined);
  const unknown = { toolName: "AskClaude", toolCallId: "unknown", input: { model: "anthropic/unknown" } };
  assert.match((await h.handlers.get("tool_call")!(unknown)).reason, /MODEL_IDENTIFIER_UNSUPPORTED/);
  assert.equal(unknown.input.model, "anthropic/unknown");
});

test("Claude invocation errors require evidence before entitlement classification", async () => {
  assert.equal(classifyClaudeInvocationError("There's an issue with the selected model. It may not exist or you may not have access to it."),
    "MODEL_INVOCATION_FAILED");
  assert.equal(classifyClaudeInvocationError("Your account does not have access to this model"),
    "MODEL_ENTITLEMENT_UNAVAILABLE");
  assert.equal(classifyClaudeInvocationError("Not logged in"), "MODEL_AUTH_UNAVAILABLE");
  assert.equal(classifyClaudeInvocationError("Invalid model identifier"), "MODEL_IDENTIFIER_UNSUPPORTED");
  const h = harness({ ok: true, status: "READY" });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" },
    { cwd: "/tmp/target", ui: { notify() {} } });
  const call = { toolName: "AskClaude", toolCallId: "claude-failure", input: {
    model: "anthropic/claude-opus-5-5", prompt: "fixture",
  } };
  await h.handlers.get("tool_result")!(routed);
  await h.handlers.get("tool_call")!(call);
  const result = await h.handlers.get("tool_result")!({ toolName: "AskClaude", toolCallId: "claude-failure",
    content: [{ type: "text", text: "Error: It may not exist or you may not have access to it." }],
    details: { error: true }, isError: false });
  assert.match(result.content.at(-1).text, /MODEL_INVOCATION_FAILED/);
  assert.doesNotMatch(result.content.at(-1).text, /MODEL_ENTITLEMENT_UNAVAILABLE/);
});

test("router-level preflight blocker is traced and keeps implementation blocked", async () => {
  for (const [details, expected] of [
    [{ ok: false, blocker_code: "BACKEND_IDENTITY_UNINITIALIZED" }, "BACKEND_IDENTITY_UNINITIALIZED"],
    [{ ok: false, result: { ok: false, blocker_code: "NO_AVAILABLE_ROUTE" } }, "NO_AVAILABLE_ROUTE"],
    [{ ok: false, blocker_code: "not a code; rm -rf" }, "ROUTING_FAILED"],
  ] as const) {
    const h = harness({ ok: true, status: "READY" });
    await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" },
      { cwd: "/tmp/target", ui: { notify() {} } });
    await h.handlers.get("tool_result")!({ toolName: "route_task", details, content: [] });
    assert.ok(h.calls.some((call) => call.input?.kind === "phase" && call.input.status === "blocked"
      && call.input.blocker_code === expected), expected);
    assert.equal(h.calls.some((call) => call.script.endsWith("backend_preflight.py")), false);
    const guard = await h.handlers.get("tool_call")!({ toolName: "bash", input: { command: "git worktree add ../x b" } });
    assert.equal(guard.block, true);
  }
});

async function startRun(h: ReturnType<typeof harness>, cwd: string) {
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd, ui: { notify() {} } });
  await h.handlers.get("tool_result")!(routed);
}

test("worker sessions never cross repositories: Examplelab -> SVC -> Examplelab", async () => {
  const h = harness({ ok: true, status: "READY" });
  await startRun(h, "/work/examplelab");
  await h.handlers.get("tool_call")!({ toolName: "spawn_subagent", toolCallId: "s1", input: { task: "inspect" } });
  await h.handlers.get("tool_result")!({ toolName: "spawn_subagent", toolCallId: "s1", input: {},
    content: [{ type: "text", text: "subagent_id: meta-1" }], details: { subagent_id: "meta-1" } });
  assert.equal(await h.handlers.get("tool_call")!({ toolName: "message_subagent", toolCallId: "m1",
    input: { subagent_id: "meta-1", prompt: "continue" } }), undefined);
  await h.handlers.get("session_shutdown")!({ reason: "new" });

  // A run on another repository cannot resume the earlier repository's worker.
  await startRun(h, "/work/sample-service");
  const crossRepo = await h.handlers.get("tool_call")!({ toolName: "message_subagent", toolCallId: "m2",
    input: { subagent_id: "meta-1", prompt: "continue" } });
  assert.equal(crossRepo.block, true);
  assert.match(crossRepo.reason, /SESSION_SCOPE_VIOLATION/);
  await h.handlers.get("session_shutdown")!({ reason: "new" });

  // Returning to the first repository starts a new run: earlier sessions stay closed.
  await startRun(h, "/work/examplelab");
  const stale = await h.handlers.get("tool_call")!({ toolName: "message_subagent", toolCallId: "m3",
    input: { subagent_id: "meta-1", prompt: "continue" } });
  assert.equal(stale.block, true);
  assert.match(stale.reason, /SESSION_SCOPE_VIOLATION/);
});

test("Claude worker outside the target repository fails closed", async () => {
  const h = harness({ ok: true, status: "READY" }, false, true, { scopeOk: false });
  await startRun(h, "/work/sample-service");
  const call = { toolName: "AskClaude", toolCallId: "c1", input: { model: "anthropic/claude-opus-5-5", prompt: "x" } };
  const result = await h.handlers.get("tool_call")!(call);
  assert.equal(result.block, true);
  assert.match(result.reason, /SESSION_REPOSITORY_MISMATCH/);
  const scope = h.calls.find((item) => item.script.endsWith("session_scope.py"));
  assert.equal(scope?.input?.target_cwd, "/work/sample-service");
  assert.equal(scope?.input?.worker_cwd, process.cwd());
  assert.equal(call.input.model, "anthropic/claude-opus-5-5", "blocked calls are not rewritten");
});

test("routed Claude workers run isolated with the assigned reasoning effort", async () => {
  const h = harness({ ok: true, status: "READY" });
  await startRun(h, "/work/examplelab");
  const shared = await h.handlers.get("tool_call")!({ toolName: "AskClaude", toolCallId: "c1",
    input: { model: "anthropic/claude-opus-5-5", prompt: "x", isolated: false } });
  assert.equal(shared.block, true);
  assert.match(shared.reason, /SESSION_ISOLATION_REQUIRED/);
  const wrongEffort = await h.handlers.get("tool_call")!({ toolName: "AskClaude", toolCallId: "c2",
    input: { model: "anthropic/claude-opus-5-5", prompt: "x", thinking: "low" } });
  assert.equal(wrongEffort.block, true);
  assert.match(wrongEffort.reason, /REASONING_EFFORT_MISMATCH: .*thinking=high/);
  const call = { toolName: "AskClaude", toolCallId: "c3", input: { model: "anthropic/claude-opus-5-5", prompt: "x" } as Record<string, unknown> };
  assert.equal(await h.handlers.get("tool_call")!(call), undefined);
  assert.deepEqual([call.input.isolated, call.input.thinking, call.input.model], [true, "high", "claude-opus-5-5"]);
  const worker = h.calls.find((item) => item.input?.kind === "worker" && item.input.step_id === "claude_worker");
  assert.equal(worker?.input?.reasoning_effort, "high");
});

test("route results without an assigned Claude effort block the Claude worker", async () => {
  const h = harness({ ok: true, status: "READY" });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: "/work/m", ui: { notify() {} } });
  const { capacity_assignments: _omitted, ...legacy } = routed.details.result;
  await h.handlers.get("tool_result")!({ ...routed, details: { result: legacy } });
  const result = await h.handlers.get("tool_call")!({ toolName: "AskClaude", toolCallId: "c1",
    input: { model: "anthropic/claude-opus-5-5", prompt: "x" } });
  assert.equal(result.block, true);
  assert.match(result.reason, /REASONING_EFFORT_UNRESOLVED/);
});

test("large context and Pi compaction preserve an active parent without custom compaction or worker gate", async () => {
  const h = harness({ ok: true, status: "READY" });
  await startRun(h, "/work/examplelab");
  let tokens = 11000;
  let compactions = 0;
  const ctx = { getContextUsage: () => ({ tokens, contextWindow: 272000, percent: null }),
    compact: () => { compactions += 1; } };
  await h.handlers.get("turn_end")!({}, ctx);
  for (tokens of [31000, 44000, 62000, 90000]) await h.handlers.get("turn_end")!({}, ctx);
  assert.equal(compactions, 0);
  assert.equal(await h.handlers.get("tool_call")!({ toolName: "spawn_subagent", toolCallId: "s1", input: { task: "x" } }), undefined);

  await h.handlers.get("session_compact")!({ fromExtension: false, reason: "manual",
    compactionEntry: { tokensBefore: 44000 } });
  await h.handlers.get("session_compact_failed")!({ reason: "threshold" });
  tokens = 20000;
  await h.handlers.get("turn_end")!({}, ctx);
  tokens = 88000;
  await h.handlers.get("turn_end")!({}, ctx);
  assert.equal(compactions, 0);
  assert.equal(await h.handlers.get("tool_call")!({ toolName: "spawn_subagent", toolCallId: "s2", input: { task: "x" } }), undefined);
  assert.equal(h.parentCalls("finish").length, 0);
  assert.equal(h.parentCalls("settle").length, 0);
  const status = await h.tools.get("orchestration_record").execute("status", { action: "status" });
  assert.equal(status.details.active, true);
  const context = h.calls.filter((call) => call.input?.kind === "context");
  assert.deepEqual(context[0].input, { kind: "context", baseline_tokens: 11000, context_tokens: 11000, working_tokens: 0 });
  assert.ok(context.some((call) => (call.input?.compaction as any)?.reason === "manual"
    && (call.input?.compaction as any)?.structured === false));
});

test("Pi compaction keeps its native summary and cut point without checkpoint work", async () => {
  const h = harness({ ok: true, status: "READY" });
  await startRun(h, "/work/examplelab");
  const message = (role: string, text: string) => ({ role, content: [{ type: "text", text }], timestamp: 1 });
  const entries = [0, 1, 2, 3].map((index) => ({ type: "message", id: `e${index}`, parentId: index ? `e${index - 1}` : null,
    timestamp: "2026-10-05T00:00:00Z", message: message(index % 2 ? "assistant" : "user", `turn ${index}`) }));
  const event = { type: "session_before_compact", reason: "manual", willRetry: false, signal: new AbortController().signal,
    branchEntries: entries, preparation: { firstKeptEntryId: "e2", messagesToSummarize: [entries[0].message, entries[1].message],
      turnPrefixMessages: [], isSplitTurn: false, tokensBefore: 40000,
      fileOps: { read: new Set(["/work/examplelab/a.py"]), written: new Set<string>(), edited: new Set(["/work/examplelab/b.py"]) },
      settings: { enabled: true, reserveTokens: 16384, keepRecentTokens: 20000 } } };
  assert.equal(h.handlers.has("session_before_compact"), false);
  assert.equal(h.calls.some((call) => call.args[0] === "checkpoint"), false);
  assert.equal(event.preparation.firstKeptEntryId, "e2");
});

test("Claude result usage maps cache writes to billed input and keeps cache reads separate", () => {
  const stdout = JSON.stringify({ type: "result", total_cost_usd: 0.42, usage: { input_tokens: 22, output_tokens: 900,
    cache_creation_input_tokens: 31000, cache_read_input_tokens: 250000 } });
  const parsed = parseClaudeResult(stdout)!;
  assert.deepEqual(parsed.usage, { input_tokens: 22, output_tokens: 900, cached_tokens: 250000,
    cache_write_tokens: 31000, cost_usd: 0.42 });
  const old = parseClaudeResult(JSON.stringify({ type: "result", usage: { input_tokens: 5, output_tokens: 1 } }))!;
  assert.equal(old.usage.cache_write_tokens, null, "an absent counter stays unknown, never zero");
});

test("the lead's own Pi-reported message cost is forwarded only when valid", async () => {
  const h = harness({ ok: true, status: "READY" });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" });
  await h.handlers.get("turn_start")!({ turnIndex: 0 });
  const ended = (timestamp: number, cost: unknown) => h.handlers.get("message_end")!({ message: { role: "assistant",
    provider: "openai-codex", model: "gpt-6.1-sol", timestamp, usage: { input: 10, output: 2, totalTokens: 12, cost } } });
  await ended(1, { total: 0.0125 });
  await ended(2, undefined);
  await ended(3, { total: -1 });
  const lead = h.parentCalls("event").filter((call) => call.input?.kind === "lead_usage").map((call) => call.input!);
  assert.equal(lead.length, 3);
  assert.equal(lead[0].estimated_cost_usd, 0.0125);
  assert.equal("estimated_cost_usd" in lead[1], false);
  assert.equal("estimated_cost_usd" in lead[2], false);
});

test("a Claude worker's cache writes reach the usage event", async () => {
  const h = harness({ ok: true, status: "READY" });
  await startRun(h, "/work/examplelab");
  await h.handlers.get("tool_result")!({ toolName: "ClaudeWorker", toolCallId: "w1", input: {}, content: [],
    details: { task_id: "t1", elapsed_ms: 10, usage: { input_tokens: 22, output_tokens: 5, cached_tokens: 400,
      cache_write_tokens: 3000, cost_usd: 0.1 } } });
  const usage = h.calls.filter((item) => item.input?.kind === "usage").map((item) => item.input!);
  assert.equal(usage.at(-1)?.cache_write_tokens, 3000);
  assert.equal(usage.at(-1)?.cached_tokens, 400);
});

test("worker usage telemetry is recorded from backend results only", async () => {
  const h = harness({ ok: true, status: "READY" });
  await startRun(h, "/work/examplelab");
  await h.handlers.get("tool_call")!({ toolName: "spawn_subagent", toolCallId: "s1", input: { task: "x" } });
  await h.handlers.get("tool_result")!({ toolName: "spawn_subagent", toolCallId: "s1", input: {}, content: [],
    details: { subagent_id: "a-1", elapsedMs: 1500, usage: { input: 900, output: 120, cacheRead: 300 } } });
  const call = { toolName: "AskClaude", toolCallId: "c1", input: { model: "anthropic/claude-opus-5-5", prompt: "x" } };
  await h.handlers.get("tool_call")!(call);
  await h.handlers.get("tool_result")!({ toolName: "AskClaude", toolCallId: "c1", input: {}, content: [],
    details: { executionTime: 4200 } });
  const usage = h.calls.filter((item) => item.input?.kind === "usage").map((item) => item.input);
  assert.deepEqual(usage, [
    { kind: "usage", backend: "pi-subagent", duration_ms: 1500, input_tokens: 900, output_tokens: 120,
      cached_tokens: 300, total_tokens: null },
    { kind: "usage", backend: "claude-code", duration_ms: 4200 },
  ]);
});

test("orchestration_record accepts escalation reasons and reasoning effort only from closed sets", () => {
  const h = harness({ ok: true, status: "READY" });
  const schema = h.tools.get("orchestration_record").parameters;
  assert.equal(Value.Check(schema, { action: "event", event: { kind: "phase", phase: "p", status: "in_progress",
    route: "CODEX", escalation_reason: "FAILED_ATTEMPTS" } }), true);
  assert.equal(Value.Check(schema, { action: "event", event: { kind: "phase", phase: "p", status: "in_progress",
    escalation_reason: "FELT_LIKE_IT" } }), false);
  assert.equal(Value.Check(schema, { action: "event", event: { kind: "worker", phase: "p", step_id: "s",
    status: "in_progress", reasoning_effort: "xhigh" } }), false);
  assert.equal(Value.Check(schema, { action: "event", event: { kind: "context", baseline_tokens: 1 } }), false,
    "context telemetry is recorded by the extension, never reported by the model");
});

test("isolation holds between parent runs: follow-up turns cannot share Claude sessions", async () => {
  const h = harness({ ok: true, status: "READY" });
  const ctx = { cwd: "/work/examplelab" };
  const shared = await h.handlers.get("tool_call")!({ toolName: "AskClaude", toolCallId: "f1",
    input: { prompt: "continue", isolated: false } }, ctx);
  assert.equal(shared.block, true);
  assert.match(shared.reason, /SESSION_ISOLATION_REQUIRED/);
  const call = { toolName: "AskClaude", toolCallId: "f2", input: { prompt: "continue" } as Record<string, unknown> };
  assert.equal(await h.handlers.get("tool_call")!(call, ctx), undefined);
  assert.equal(call.input.isolated, true);

  const unknown = await h.handlers.get("tool_call")!({ toolName: "message_subagent", toolCallId: "f3",
    input: { subagent_id: "restored-from-disk", prompt: "go on" } }, ctx);
  assert.match(unknown.reason, /SESSION_SCOPE_UNVERIFIED/);
  await h.handlers.get("tool_result")!({ toolName: "spawn_subagent", toolCallId: "f4", input: {}, content: [],
    details: { subagent_id: "here-1" } }, ctx);
  assert.equal(await h.handlers.get("tool_call")!({ toolName: "message_subagent", toolCallId: "f5",
    input: { subagent_id: "here-1", prompt: "go on" } }, ctx), undefined);
});

test("a subagent spawned in another repository cannot be continued between runs", async () => {
  const h = harness({ ok: true, status: "READY" }, false, true, { scopeOk: false });
  await h.handlers.get("tool_result")!({ toolName: "spawn_subagent", toolCallId: "s", input: {}, content: [],
    details: { subagent_id: "meta-1" } }, { cwd: "/work/examplelab" });
  const result = await h.handlers.get("tool_call")!({ toolName: "message_subagent", toolCallId: "m",
    input: { subagent_id: "meta-1", prompt: "x" } }, { cwd: "/work/sample-service" });
  assert.match(result.reason, /SESSION_REPOSITORY_MISMATCH/);
});

test("binding rejects paths that are not linked worktrees of the target", async () => {
  const h = harness({ ok: true, status: "READY" }, false, true, { worktreeOk: false });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: "/work/examplelab", ui: { notify() {} } });
  const result = await h.tools.get("orchestration_record").execute("b",
    { action: "bind_workspace", task_id: "S1", step_id: "claude_worker", path: "/work/examplelab" });
  assert.equal(result.details.ok, false);
  assert.match(result.content[0].text, /WORKER_WORKSPACE_INVALID/);
});

test("a worker that writes to the parent checkout blocks further workers", async () => {
  const h = harness({ ok: true, status: "READY" });
  await startRun(h, "/work/examplelab");
  await h.handlers.get("tool_call")!({ toolName: "spawn_subagent", toolCallId: "w1", input: { task: "x" } });
  h.checkout.digest = "dirty";
  h.checkout.status = [" M src/store.py"];
  const result = await h.handlers.get("tool_result")!({ toolName: "spawn_subagent", toolCallId: "w1", input: {},
    content: [{ type: "text", text: "done" }], details: { subagent_id: "w-1" } });
  assert.match(result.content.at(-1).text, /WORKER_SCOPE_VIOLATION.* M src\/store\.py/);
  assert.ok(h.calls.some((call) => call.input?.kind === "phase" && call.input.blocker_code === "WORKER_SCOPE_VIOLATION"));
  const next = await h.handlers.get("tool_call")!({ toolName: "spawn_subagent", toolCallId: "w2", input: { task: "y" } });
  assert.match(next.reason, /WORKER_SCOPE_VIOLATION/);
  const worktree = await h.handlers.get("tool_call")!({ toolName: "bash", toolCallId: "g", input: { command: "git worktree add ../x" } });
  assert.equal(worktree.block, true);
});

test("status text states ok and active, since the model never sees details", async () => {
  const h = harness({ ok: true });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" });
  const status = await h.tools.get("orchestration_record").execute("status", { action: "status" });
  assert.match(status.content[0].text, /is ready \(ok: true, active: true\)/);
  assert.equal(status.details.active, true);
});

test("a completion refusal names what is unfinished so the lead fixes it in one step", async () => {
  const detail = "unfinished: phase install-repair is in_progress. Record each as completed, blocked or failed, then finish again";
  const h = harness({ ok: true }, false, true, { completionError: "PARENT_COMPLETION_INCOMPLETE", completionDetail: detail });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" });
  const refused = await h.tools.get("orchestration_record").execute("finish", { action: "finish", status: "COMPLETED" });
  assert.equal(refused.details.ok, false);
  assert.match(refused.content[0].text, /PARENT_COMPLETION_INCOMPLETE\): unfinished: phase install-repair is in_progress/);
  const other = harness({ ok: true }, false, true, { completionError: "PARENT_STORAGE_UNAVAILABLE", completionDetail: "secret-ish detail" });
  await other.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" });
  const generic = await other.tools.get("orchestration_record").execute("finish", { action: "finish", status: "COMPLETED" });
  assert.doesNotMatch(generic.content[0].text, /secret-ish/);
});
