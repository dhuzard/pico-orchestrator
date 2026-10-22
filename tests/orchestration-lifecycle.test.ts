import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Value } from "typebox/value";
import { createOrchestrationLifecycle, leaseOwner, leaseProcessState, runJson, scopesOverlap } from "../extensions/orchestration-lifecycle.ts";
import { hostname } from "node:os";
import { classifyClaudeInvocationError, resolveClaudeBackendModel } from "../extensions/claude-backend-adapter.ts";
import { buildWorkerEnv, claudeWorkerArgs, describeDenials, detectWorkerSandbox, inGitDirectory, parseClaudeResult,
  runClaudeWorker, SANDBOX_CREDENTIAL_PATHS, WORKER_ENV_CREDENTIALS, WORKER_ENV_NAMES, WorkerConfigError,
  workerSandboxConfig, workerSandboxEnabled, workerSandboxPosture, workerSandboxSettings } from "../extensions/claude-worker.ts";
import type { SandboxProbe } from "../extensions/claude-worker.ts";

type Handler = (event: any, context?: any) => Promise<any> | any;

function harness(preflight: Record<string, unknown>, failEvent = false, verifyOk = true,
                 options: { scopeOk?: boolean; worktreeOk?: boolean; worktreeBlocker?: string; entries?: any[];
                   parentStatus?: string; sessionId?: string; settleFails?: boolean; completionDetail?: string;
                   completionError?: string; limits?: Record<string, unknown>; executable?: string;
                   routeConflict?: boolean; reroutePhases?: string[]; waveEventResult?: Record<string, unknown>;
                   workerRestatementResult?: Record<string, unknown>;
                   escalationWorkerConflict?: boolean; resumeResult?: Record<string, unknown>;
                   heartbeatResult?: Record<string, unknown>; eventResult?: Record<string, unknown>;
                   clock?: () => number; failKind?: string; sandboxProbe?: SandboxProbe;
                   gateRefusal?: (input: Record<string, unknown>) => Record<string, unknown> | null } = {}) {
  const handlers = new Map<string, Handler>();
  const tools = new Map<string, any>();
  const calls: { script: string; args: string[]; input?: Record<string, unknown> }[] = [];
  // Parent checkout state as seen by the fake snapshot operation.
  const checkout = { digest: "clean", status: [] as string[], head: "a".repeat(40), heads: {} as Record<string, string> };
  let escalated = false;
  const messages: { message: any; options?: any }[] = [];
  const command = async (script: string, args: string[], input?: Record<string, unknown>) => {
    calls.push({ script, args, input });
    if (script.endsWith("run_lease.py")) return options.heartbeatResult ?? { ok: true, state: "live" };
    if (script.endsWith("session_scope.py")) {
      if (input?.operation === "head") return { ok: true, head: checkout.heads[String(input.path)] ?? checkout.head };
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
    if (args[0] === "resume" && options.resumeResult) return options.resumeResult;
    if (args[0] === "status" || args[0] === "resume") return { run_id: args[1], status: options.parentStatus ?? "IN_PROGRESS",
      reroute_phases: options.reroutePhases ?? [] };
    if (args[0] === "settle" && options.settleFails) return { ok: false, error_code: "PARENT_STORAGE_UNAVAILABLE" };
    if (args[0] === "event" && input?.kind === "retry_gate" && options.failKind !== "retry_gate") {
      const refusal = options.gateRefusal?.(input) ?? null;
      return { ok: true, retry_gate: refusal ? { refused: true, ...refusal } : { refused: false } };
    }
    if (args[0] === "event" && options.failKind && input?.kind === options.failKind) {
      return { ok: false, error_code: "PARENT_STORAGE_UNAVAILABLE" };
    }
    if (args[0] === "event" && failEvent) return { ok: false };
    if (args[0] === "event" && options.eventResult) return options.eventResult;
    if (args[0] === "event" && options.waveEventResult && input?.kind === "wave") return options.waveEventResult;
    if (args[0] === "event" && options.workerRestatementResult && input?.kind === "worker" && !input.only_if_open && input.status === "completed")
      return options.workerRestatementResult;
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
    sendMessage(message: any, sendOptions?: any) { messages.push({ message, options: sendOptions }); },
    appendEntry(customType: string, data: unknown) { entries.push({ type: "custom", customType, data }); },
    getActiveTools() { return ["route_task", "spawn_subagent", "ClaudeWorker", "orchestration_record"]; },
  } as unknown as ExtensionAPI;
  // The host's real sandbox prerequisites never decide a test: the probe is the fake's (available unless overridden).
  createOrchestrationLifecycle(command, options.clock,
    options.sandboxProbe ?? (() => ({ available: true, detail: "test probe" })))(pi);
  const parentCalls = (operation: string) =>
    calls.filter((call) => call.script.endsWith("parent_orchestration.py") && call.args[0] === operation);
  const leaseCalls = (operation: string) =>
    calls.filter((call) => call.script.endsWith("run_lease.py") && call.args[0] === operation);
  return { handlers, tools, calls, parentCalls, leaseCalls, checkout, entries, messages };
}

const CODEX_HIGH = "openai-codex/gpt-6.1-sol";
function routeFor(id: string, options: { workerMutation?: "allowed" | "forbidden"; specifierMutation?: "allowed"; specifierEffort?: string;
  reviewerEffort?: string; specifierTier?: string } = {}) {
  return {
    toolName: "route_task",
    toolCallId: `route-${id}`,
    details: { result: { ok: true, classification: { base: { id } },
      selected_route: "CODEX_CLAUDE_CODEX", policy_version: 7,
      execution_plan: { steps: [{ id: "codex_specifier", family: "codex", mutation_permission: options.specifierMutation ?? "forbidden" },
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
  assert.equal(status.details.active, true);
  assert.match(status.content[0].text, /Rerun route_task/);
  // A resumed lead stops on BLOCKED (RESUME_PROMPT); a required reroute is its next step, not a stop (#69).
  assert.match(status.content[0].text, /^NEXT STEP \(PARENT_ROUTE_RECHECK_REQUIRED\): the run is active and not stopped\./);
  assert.doesNotMatch(status.content[0].text, /BLOCKED/);
  assert.doesNotMatch(status.content[0].text, /is ready/);
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
  assert.match(status.content[0].text, /for B, then/);
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
  if (prompt.includes("FAIL")) { process.stdout.write(JSON.stringify({ type: "result", subtype: "error", is_error: true, result: "boom" })); process.exit(1); }
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
      status: "in_progress", only_if_open: true, model: "anthropic/claude-opus-5-5", backend: "claude-code", backend_model: "claude-opus-5-5",
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
  assert.ok(!args.includes("--settings"), "settings are passed only when the caller resolves them");
  const settings = { sandbox: { enabled: true } };
  const sandboxed = claudeWorkerArgs({ executable: "claude", cwd, backendModel: "haiku", effort: "low", mode: "full" }, settings);
  assert.deepEqual(sandboxed.slice(-2), ["--settings", JSON.stringify(settings)]);
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
  // The sandbox is on by default (#63); `0` is the only way off, and it is an explicit, recorded opt-out.
  assert.equal(workerSandboxEnabled({}), true);
  assert.deepEqual(workerSandboxPosture({}), { state: "enabled", source: "default" });
  assert.deepEqual(workerSandboxPosture({ PI_ORCHESTRATOR_WORKER_SANDBOX: "" }), { state: "enabled", source: "default" });
  assert.deepEqual(workerSandboxPosture({ PI_ORCHESTRATOR_WORKER_SANDBOX: "1" }), { state: "enabled", source: "env" });
  assert.deepEqual(workerSandboxPosture({ PI_ORCHESTRATOR_WORKER_SANDBOX: "0" }), { state: "opted_out", source: "env" });
  assert.equal(workerSandboxEnabled({ PI_ORCHESTRATOR_WORKER_SANDBOX: "0" }), false);
  for (const bad of ["yes", "true", "2", "off"]) {
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
    // Default on: the worker gets the sandbox settings without any variable set.
    const defaults = JSON.parse(echoed.args[echoed.args.indexOf("--settings") + 1]);
    assert.equal(defaults.sandbox.enabled, true);
    assert.deepEqual(plain.details.sandbox, { state: "enabled", source: "default", applied: true,
      allowed_domains: [], cache_directories: [] });
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
    assert.equal(JSON.parse(echoedExtended.args[echoedExtended.args.indexOf("--settings") + 1]).sandbox.failIfUnavailable, true);

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

test("a ClaudeWorker result records the worker's terminal status without the lead", async () => {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "pico-claude-terminal-")));
  const workspace = join(dir, "wt");
  mkdirSync(workspace);
  try {
    const h = harness({ ok: true, status: "READY" }, false, true, { executable: fakeClaude(dir) });
    await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: dir, ui: { notify() {} } });
    await h.handlers.get("tool_result")!(routeFor("A", { workerMutation: "allowed" }));
    await h.tools.get("orchestration_record").execute("b", { action: "bind_workspace", task_id: "A",
      step_id: "claude_worker", path: workspace });
    const terminal = () => h.parentCalls("event").map((call) => call.input!)
      .filter((input) => input.kind === "worker" && ["completed", "failed", "blocked"].includes(String(input.status)));
    const ok = await claudeWorkerRun(h, "ok", { prompt: "[pico-task A:claude_worker] go" });
    assert.equal(ok.result!.isError, false);
    assert.deepEqual(terminal(), [{ kind: "worker", phase: "A", step_id: "claude_worker", only_if_open: true, status: "completed" }]);
    const bad = await claudeWorkerRun(h, "bad", { prompt: "[pico-task A:claude_worker] FAIL" });
    assert.equal(bad.result!.isError, true);
    const failed = terminal().at(-1)!;
    assert.deepEqual([failed.phase, failed.step_id, failed.status], ["A", "claude_worker", "failed"]);
    assert.equal(failed.only_if_open, true, "the automatic record never overrides the lead");
    assert.match(String(failed.blocker_code), /^[A-Z][A-Z0-9_]{0,99}$/);
    // Every write carries the lease owner.
    assert.ok(h.parentCalls("event").every((call) => call.args.includes("--owner")));
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
    const aborted = h.parentCalls("event").map((call) => call.input!).filter((input) => input.kind === "worker").at(-1)!;
    assert.deepEqual([aborted.status, aborted.blocker_code], ["blocked", "WORKER_ABORTED"]);
    // The kill is asynchronous: a dying or not yet reaped child still answers signal 0 (#41).
    const gone = () => { try { process.kill(pid, 0); return false; } catch (error) { return (error as NodeJS.ErrnoException).code === "ESRCH"; } };
    for (let tries = 0; tries < 100 && !gone(); tries += 1) await new Promise((done) => setTimeout(done, 20));
    assert.equal(gone(), true, "the Claude Code child is gone");
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
    provider: "openai-codex", model: "gpt-6.1-sol", timestamp, usage: { input: 10, output: 2, totalTokens: 12, cacheWrite: 7, cost } } });
  await ended(1, { total: 0.0125 });
  await ended(2, undefined);
  await ended(3, { total: -1 });
  const lead = h.parentCalls("event").filter((call) => call.input?.kind === "lead_usage").map((call) => call.input!);
  assert.equal(lead.length, 3);
  assert.equal(lead[0].estimated_cost_usd, 0.0125);
  assert.equal(lead[0].cache_write_tokens, 7);
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
    details: { subagent_id: "a-1", elapsedMs: 1500, usage: { input: 900, output: 120, cacheRead: 300, cacheWrite: 40 } } });
  const call = { toolName: "AskClaude", toolCallId: "c1", input: { model: "anthropic/claude-opus-5-5", prompt: "x" } };
  await h.handlers.get("tool_call")!(call);
  await h.handlers.get("tool_result")!({ toolName: "AskClaude", toolCallId: "c1", input: {}, content: [],
    details: { executionTime: 4200 } });
  const usage = h.calls.filter((item) => item.input?.kind === "usage").map((item) => item.input);
  assert.deepEqual(usage, [
    { kind: "usage", backend: "pi-subagent", duration_ms: 1500, input_tokens: 900, output_tokens: 120,
      cached_tokens: 300, total_tokens: null, cache_write_tokens: 40 },
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

function restoredSession(overrides: Record<string, unknown> = {}) {
  const entries: any[] = [{ type: "custom", customType: "pi-orchestration-parent-v1",
    data: { run_id: "parent-test", session_id: "session-a", target_cwd: process.cwd(), active: true } }];
  const context = { cwd: process.cwd(), ui: { notify() {} },
    sessionManager: { getSessionId: () => "session-a", getBranch: () => entries } };
  const h = harness({ ok: true, status: "READY" }, false, true, { entries, ...overrides });
  return { h, context, entries };
}

const thisOwner = (sessionId: string | null) => {
  const state = leaseProcessState();
  return leaseOwner(sessionId, state.launcherOwner, process.pid, hostname(), state.nonce);
};

test("lease owner is the launcher's when handed down, else unique per host, process nonce and session", () => {
  assert.equal(leaseOwner("s", "headless-abc-123", 7, "box", "n1"), "headless-abc-123");
  assert.equal(leaseOwner("session-a", null, 42, "my host", "a1b2"), "pi-my-host-42-a1b2-session-a");
  // Same pid and session in another process (pid reuse, shared namespace) still differs by nonce.
  assert.notEqual(leaseOwner("session-a", null, 42, "box", "n1"), leaseOwner("session-a", null, 42, "box", "n2"));
  assert.notEqual(leaseOwner("session-a", null, 42, "box", "n1"), leaseOwner("session-a", null, 43, "box", "n1"));
  assert.match(leaseOwner(null, "../x", 1, "box", "n1"), /^pi-box-1-n1-nosession$/);
});

test("the handed-down owner is read once, removed from the environment, and survives a reload", () => {
  const state = leaseProcessState();
  const nonce = state.nonce;
  const env: NodeJS.ProcessEnv = { PI_ORCHESTRATION_LEASE_OWNER: "headless-abc-123" };
  try {
    assert.equal(leaseProcessState(env).launcherOwner, "headless-abc-123");
    assert.equal("PI_ORCHESTRATION_LEASE_OWNER" in env, false, "nested processes must not inherit the owner");
    // A reloaded extension instance sees the same process identity.
    assert.equal(leaseProcessState({}).launcherOwner, "headless-abc-123");
    assert.equal(leaseProcessState({}).nonce, nonce);
  } finally {
    state.launcherOwner = null;
  }
});

test("a launched process resumes its launcher's lease only through the explicit hand-off", async () => {
  const state = leaseProcessState();
  state.launcherOwner = "headless-abc-123";
  try {
    const { h, context } = restoredSession();
    await h.handlers.get("session_start")!({ reason: "startup" }, context);
    const resume = h.parentCalls("resume")[0].args;
    assert.equal(resume[resume.indexOf("--owner") + 1], "headless-abc-123");
    assert.ok(resume.includes("--handoff"));
  } finally {
    state.launcherOwner = null;
  }
  const { h, context } = restoredSession();
  await h.handlers.get("session_start")!({ reason: "startup" }, context);
  assert.equal(h.parentCalls("resume")[0].args.includes("--handoff"), false);
});

test("a new run and a resumed run both take the lease as this process", async () => {
  const fresh = harness({ ok: true });
  await fresh.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" });
  const start = fresh.parentCalls("start")[0].args;
  assert.equal(start[start.indexOf("--pid") + 1], String(process.pid));
  assert.match(start[start.indexOf("--owner") + 1], /^(pi-|headless-)/);
  const { h, context } = restoredSession();
  await h.handlers.get("session_start")!({ reason: "startup" }, context);
  assert.equal(h.parentCalls("status").length, 0);
  const resume = h.parentCalls("resume")[0].args;
  assert.equal(resume[1], "parent-test");
  assert.equal(resume[resume.indexOf("--worktree") + 1], process.cwd());
  assert.equal(resume[resume.indexOf("--owner") + 1], thisOwner("session-a"));
  const status = await h.tools.get("orchestration_record").execute("status", { action: "status" });
  assert.equal(status.details.active, true);
});

for (const code of ["RUN_LEASE_HELD", "RUN_LEASE_STALE", "RESUME_CHECKPOINT_DRIFT", "RESUME_CHECKPOINT_MISSING"]) {
  test(`a refused resume (${code}) fails closed before any worker, event, or settle`, async () => {
    const { h, context } = restoredSession({ resumeResult: { ok: false, error_code: code, run_id: "parent-test" } });
    await h.handlers.get("session_start")!({ reason: "startup" }, context);
    const status = await h.tools.get("orchestration_record").execute("status", { action: "status" });
    assert.equal(status.details.ok, false);
    assert.equal(status.details.active, false);
    assert.match(status.content[0].text, new RegExp(code));
    const worker = await h.handlers.get("tool_call")!({ toolName: "spawn_subagent", toolCallId: "w",
      input: { label: "S1:codex_worker", task: "x" } });
    assert.equal(worker.block, true);
    const worktree = await h.handlers.get("tool_call")!({ toolName: "bash", toolCallId: "wt",
      input: { command: "git worktree add ../x" } });
    assert.equal(worktree.block, true);
    const replacement = await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, context);
    assert.match(replacement.message.content, new RegExp(code));
    // Another owner's run is never settled, written to, or replaced by this process.
    await h.handlers.get("session_shutdown")!({ reason: "quit" });
    assert.equal(h.parentCalls("settle").length, 0);
    assert.equal(h.parentCalls("event").length, 0);
    assert.equal(h.parentCalls("start").length, 0);
    assert.equal(h.leaseCalls("heartbeat").length, 0);
  });
}

test("the active run renews its lease each turn and stops renewing at an explicit outcome", async () => {
  const h = harness({ ok: true });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" });
  await h.handlers.get("turn_end")!({}, { cwd: "/tmp/target", getContextUsage: () => undefined });
  const beat = h.leaseCalls("heartbeat");
  assert.equal(beat.length, 1);
  assert.equal(beat[0].args[1], "parent-test");
  assert.equal(beat[0].args[beat[0].args.indexOf("--pid") + 1], String(process.pid));
  const finished = await h.tools.get("orchestration_record").execute("finish", { action: "finish", status: "PARTIAL" });
  assert.equal(finished.details.ok, true);
  await h.handlers.get("turn_end")!({}, { cwd: "/tmp/target", getContextUsage: () => undefined });
  assert.equal(h.leaseCalls("heartbeat").length, 1);
});

test("a lost lease detaches this process from the run without settling it", async () => {
  const h = harness({ ok: true }, false, true,
    { heartbeatResult: { ok: false, error_code: "RUN_LEASE_LOST", run_id: "parent-test" } });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" });
  await h.handlers.get("turn_end")!({}, { cwd: "/tmp/target", getContextUsage: () => undefined });
  const worker = await h.handlers.get("tool_call")!({ toolName: "spawn_subagent", toolCallId: "w",
    input: { label: "S1:codex_worker", task: "x" } });
  assert.equal(worker.block, true);
  const status = await h.tools.get("orchestration_record").execute("status", { action: "status" });
  assert.equal(status.details.active, false);
  assert.match(status.content[0].text, /RUN_LEASE_LOST/);
  await h.handlers.get("session_shutdown")!({ reason: "quit" });
  assert.equal(h.parentCalls("settle").length, 0);
  assert.equal(h.parentCalls("finish").length, 0);
});

test("a transient heartbeat failure is diagnosed without detaching", async () => {
  const h = harness({ ok: true }, false, true,
    { heartbeatResult: { ok: false, error_code: "RUN_LEASE_LOCK_TIMEOUT" } });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" });
  await h.handlers.get("turn_end")!({}, { cwd: "/tmp/target", getContextUsage: () => undefined });
  const status = await h.tools.get("orchestration_record").execute("status", { action: "status" });
  assert.equal(status.details.active, true);
});

test("worker approval and workspace binding record a resume checkpoint", async () => {
  const h = harness({ ok: true, status: "READY" });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" });
  const route = routeFor("S1");
  await h.handlers.get("tool_call")!({ toolName: "route_task", toolCallId: route.toolCallId,
    input: { classified_task: { id: "S1" } } });
  await h.handlers.get("tool_result")!(route);
  const checkpoints = () => h.parentCalls("event").filter((call) => call.input?.kind === "checkpoint");
  assert.deepEqual(checkpoints().map((call) => call.input), [{ kind: "checkpoint", phase: "S1", reason: "worker_approval" }]);
  const bound = await h.tools.get("orchestration_record").execute("bind", { action: "bind_workspace",
    task_id: "S1", step_id: "codex_worker", path: "/tmp/target-wt" });
  assert.equal(bound.details.ok, true);
  assert.equal(checkpoints().length, 2);
});

test("every write to the run names this process as its owner", async () => {
  const h = harness({ ok: true });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" });
  const tool = h.tools.get("orchestration_record");
  await tool.execute("e", { action: "event", event: { kind: "phase", phase: "S1", status: "in_progress" } });
  await tool.execute("f", { action: "finish", status: "PARTIAL" });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" });
  await h.handlers.get("session_shutdown")!({ reason: "quit" });
  const writes = h.calls.filter((call) => ["event", "finish", "settle"].includes(call.args[0])
    || (call.script.endsWith("context_budget.py") && call.args[0] === "start"));
  assert.ok(writes.length >= 4);
  for (const call of writes) assert.equal(call.args[call.args.indexOf("--owner") + 1], thisOwner(null), call.args.join(" "));
});

test("a write refused because another owner holds the lease detaches without settling", async () => {
  const h = harness({ ok: true }, false, true, { eventResult: { ok: false, error_code: "RUN_LEASE_LOST" } });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" });
  const tool = h.tools.get("orchestration_record");
  await h.handlers.get("tool_result")!(routed);
  const status = await tool.execute("s", { action: "status" });
  assert.equal(status.details.active, false);
  assert.match(status.content[0].text, /RUN_LEASE_LOST/);
  await h.handlers.get("session_shutdown")!({ reason: "quit" });
  assert.equal(h.parentCalls("settle").length, 0);
});

test("a refused repeat worker recode is reported to the lead without blocking the run", async () => {
  for (const [code, blocks] of [["PARENT_WORKER_RECODE_REPEATED", false], ["PARENT_WORKER_OVERRIDE_LIMIT", false],
                                ["PARENT_EVENT_CONFLICT", true]] as const) {
    const h = harness({ ok: true }, false, true, { eventResult: { ok: false, error_code: code } });
    await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" });
    const tool = h.tools.get("orchestration_record");
    const refused = await tool.execute("e", { action: "event", event: { kind: "worker", phase: "p",
      step_id: "claude_worker", status: "blocked", blocker_code: "CODE_A" } });
    assert.match(refused.content[0].text, new RegExp(code), "the lead is told");
    assert.equal(/^BLOCKED/.test(refused.content[0].text), blocks);
    const status = await tool.execute("s", { action: "status" });
    assert.equal(status.details.ok, !blocks, `${code}: status after the refusal`);
  }
});

test("renewals failing past the lease lifetime detach the process and abort running workers", async () => {
  let now = 1_000_000;
  const h = harness({ ok: true }, false, true,
    { heartbeatResult: { ok: false, error_code: "RUN_LEASE_LOCK_TIMEOUT" }, clock: () => now });
  let aborted = 0;
  const ctx = { cwd: "/tmp/target", ui: { notify() {} }, abort() { aborted += 1; }, getContextUsage: () => undefined };
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, ctx);
  // Transient failures inside the lease lifetime keep the run.
  now += 120_000;
  await h.handlers.get("turn_end")!({}, ctx);
  assert.equal((await h.tools.get("orchestration_record").execute("s", { action: "status" })).details.active, true);
  // An exploration worker is running when the lease can no longer be trusted.
  assert.equal(await h.handlers.get("tool_call")!({ toolName: "spawn_subagent", toolCallId: "w1",
    input: { task: "explore" } }, ctx), undefined);
  now += 121_000; // 241 s without a successful renewal: past TTL (300 s) minus the 60 s margin
  const next = await h.handlers.get("tool_call")!({ toolName: "spawn_subagent", toolCallId: "w2",
    input: { task: "explore more" } }, ctx);
  assert.equal(next.block, true);
  assert.equal(aborted, 1, "the running worker is aborted, not only new tool calls");
  const status = await h.tools.get("orchestration_record").execute("s", { action: "status" });
  assert.equal(status.details.active, false);
  assert.match(status.content[0].text, /RUN_LEASE_SELF_EXPIRED/);
  await h.handlers.get("session_shutdown")!({ reason: "quit" });
  assert.equal(h.parentCalls("settle").length, 0);
});

test("a successful renewal extends how long the process may act", async () => {
  let now = 1_000_000;
  const h = harness({ ok: true }, false, true,
    { heartbeatResult: { ok: true, state: "live", lease: { ttl_seconds: 300 } }, clock: () => now });
  const ctx = { cwd: "/tmp/target", ui: { notify() {} }, abort() {}, getContextUsage: () => undefined };
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, ctx);
  for (let turn = 0; turn < 5; turn += 1) {
    now += 200_000;
    await h.handlers.get("turn_end")!({}, ctx);
  }
  assert.equal(await h.handlers.get("tool_call")!({ toolName: "spawn_subagent", toolCallId: "w",
    input: { task: "explore" } }, ctx), undefined);
});

// ---- Lead overhead (issue #43): what the lead must record, before and after ----

const REPLAY = `
import json, shutil, subprocess, sys, tempfile
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root / "tools"))
import parent_orchestration as po
import provenance
payload = json.load(sys.stdin)
with tempfile.TemporaryDirectory() as tmp:
    records = Path(tmp) / "orchestrator"
    (records / "schemas").mkdir(parents=True)
    shutil.copy(root / "schemas/run-record.schema.json", records / "schemas/run-record.schema.json")
    target = Path(tmp) / "target"
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    subprocess.run(["git", "-C", str(target), "remote", "add", "origin", "git@github.com:Examplecorp/examplelab.git"], check=True)
    run_id = po.start(worktree=target, repo_root=records)["run_id"]
    errors = []
    for event in payload["events"]:
        try:
            po.record_event(run_id, event, repo_root=records)
        except po.ParentRunError as exc:
            errors.append([event.get("kind"), getattr(exc, "error_code", None), str(exc)])
    final = None
    if payload.get("finish"):
        try:
            final = po.finish(run_id, payload["finish"], repo_root=records)["outcome"]
        except po.ParentRunError as exc:
            errors.append(["finish", getattr(exc, "error_code", None), str(exc)])
    print(json.dumps({"errors": errors, "final": final, "parent": provenance.get_run(records, run_id)["parent_orchestration"]}))
`;

/** Replay the parent events the harness saw into the real recorder; returns the resulting parent record. */
function replay(events: unknown[], finish: string | null) {
  const root = join(import.meta.dirname, "..");
  const python = existsSync(join(root, ".venv/bin/python")) ? join(root, ".venv/bin/python") : "python3";
  const done = spawnSync(python, ["-I", "-c", REPLAY, root], { input: JSON.stringify({ events, finish }), encoding: "utf8" });
  assert.equal(done.status, 0, done.stderr);
  return JSON.parse(done.stdout) as { errors: unknown[][]; final: string | null; parent: any };
}
const eventsOf = (h: ReturnType<typeof harness>) => h.parentCalls("event").map((call) => call.input);

/**
 * A scripted lead for a two-task run shaped like the CRT pilot: each task has a Codex specifier, a mutating
 * Claude worker and a Codex reviewer, run one task at a time. `legacy` records what the previous prompt required
 * (inspection phase, a declared wave per task, a directive and a feedback for every step); `lean` records what
 * the current prompt requires.
 */
async function twoTaskRun(mode: "legacy" | "lean") {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "pico-lead-overhead-")));
  try {
    const h = harness({ ok: true, status: "READY" }, false, true, { executable: fakeClaude(dir) });
    await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: dir, ui: { notify() {} } });
    const record = h.tools.get("orchestration_record");
    let leadRecordCalls = 0;
    const lead = async (params: Record<string, unknown>) => {
      leadRecordCalls += 1;
      const result = await record.execute(`r${leadRecordCalls}`, params);
      assert.equal(result.details.ok, true, JSON.stringify(params) + result.content[0].text);
      return result;
    };
    const interaction = (type: string, task: string, step: string, summary: string) =>
      lead({ action: "event", event: { kind: "interaction", type, task_id: task, step_id: step, summary } });
    await lead({ action: "status" });
    if (mode === "legacy") {
      await lead({ action: "event", event: { kind: "phase", phase: "inspection", status: "in_progress" } });
      await lead({ action: "event", event: { kind: "phase", phase: "inspection", status: "completed" } });
    }
    for (const task of ["A", "B"]) {
      await h.handlers.get("tool_result")!(routeFor(task, { workerMutation: "allowed" }));
      const workspace = join(dir, `wt-${task}`);
      mkdirSync(workspace);
      await lead({ action: "bind_workspace", task_id: task, step_id: "claude_worker", path: workspace });
      if (mode === "legacy") {
        await lead({ action: "declare_wave", wave_id: `wave-${task}`, tasks: [{ task_id: task, write_scope: [`${task}/**`] }] });
      }
      for (const [step, tool] of [["codex_specifier", "spawn_subagent"], ["claude_worker", "ClaudeWorker"], ["codex_reviewer", "spawn_subagent"]]) {
        if (mode === "legacy") await interaction("directive", task, step, `Run ${step}`);
        const id = `${task}-${step}`;
        if (tool === "ClaudeWorker") {
          const run = await claudeWorkerRun(h, id, { prompt: `[pico-task ${task}:${step}] implement in ${workspace}` });
          assert.equal(run.guard, undefined);
        } else {
          const input = { task: `${step} for ${task}`, label: `${task}:${step}` };
          assert.equal(await h.handlers.get("tool_call")!({ toolName: tool, toolCallId: id, input }), undefined);
          await h.handlers.get("tool_result")!({ toolName: tool, toolCallId: id,
            content: [{ type: "text", text: `${step} of ${task} committed at 244fea7; 80 passes, one skip` }],
            details: { subagent_id: `sub-${id}`, elapsedMs: 5 } });
        }
        if (mode === "legacy") await interaction("feedback", task, step, `${step} returned`);
      }
      if (task === "A") await interaction("decision", "A", "codex_reviewer", "Review passed; start the next task");
      await lead({ action: "event", event: { kind: "worker", phase: task, step_id: "codex_reviewer",
        status: "completed", model: CODEX_HIGH } });
      await lead({ action: "event", event: { kind: "phase", phase: task, status: "completed", route: "CODEX_CLAUDE_CODEX" } });
      const wave = mode === "legacy" ? `wave-${task}` : "auto";
      await lead({ action: "integrate_begin", wave_id: wave });
      await lead({ action: "integrate_end", wave_id: wave });
    }
    await lead({ action: "finish", status: "COMPLETED" });
    return { h, leadRecordCalls, replayed: replay(eventsOf(h), "COMPLETED") };
  } finally { rmSync(dir, { recursive: true, force: true }); }
}

/** The audit facts of a parent record, with wave ids (a lead's choice or generated) masked. */
function facts(parent: any) {
  return {
    activity: parent.activity.map((entry: any) => `${entry.status} ${entry.tool} ${entry.task_id}:${entry.step_id}`).sort(),
    routedStarts: parent.activity.filter((entry: any) => entry.status === "started")
      .map((entry: any) => `${entry.task_id}:${entry.step_id}:${entry.route}`).sort(),
    waves: parent.waves.map((wave: any) => `${wave.task_ids.join(",")}:${wave.status}`),
    phases: parent.phases.filter((phase: any) => phase.id !== "inspection").map((phase: any) =>
      `${phase.id}:${phase.status}:${phase.route}:${phase.wave_id ? "waved" : "no-wave"}:` +
      phase.workers.map((worker: any) => `${worker.step_id}=${worker.status}`).join(",")),
    checkpoint: parent.checkpoint.reason,
    final: parent.final_status,
    // What the old directive and feedback events carried, now from activity: the contract start at launch
    // and the result summary, digest and commit at return, for every worker call.
    contracts: parent.activity.filter((entry: any) => entry.status === "started")
      .map((entry: any) => `${entry.task_id}:${entry.step_id}:${entry.prompt_excerpt ? "excerpt" : "MISSING"}:${entry.prompt_digest ? "digest" : "MISSING"}`).sort(),
    results: parent.activity.filter((entry: any) => entry.status === "finished" || entry.status === "failed")
      .map((entry: any) => `${entry.task_id}:${entry.step_id}:${entry.result_summary ? "summary" : "MISSING"}:${entry.result_digest ? "digest" : "MISSING"}`).sort(),
    decisions: parent.interactions.filter((entry: any) => entry.type === "decision").map((entry: any) => entry.summary),
  };
}

/**
 * The record calls a lead must make for the two-task run, derived from what the prompt requires rather than
 * from the script: a status check, then per task a bind (the only mutating step), the reviewer's outcome
 * (mandatory review), the phase outcome and the integrate bracket, then one decision and finish. The
 * previous prompt additionally required an inspection phase pair, a declared wave per task, and a directive
 * and a feedback for each step.
 */
function requiredLeadCalls(requires: { inspectionPhase: boolean; singleTaskWave: boolean; directiveFeedback: boolean }) {
  const tasks = 2, steps = 3;
  const perTask = 1 + (requires.singleTaskWave ? 1 : 0) + (requires.directiveFeedback ? 2 * steps : 0) + 1 + 1 + 2;
  return 1 + (requires.inspectionPhase ? 2 : 0) + tasks * perTask + 1 + 1;
}

test("a two-task run needs fewer lead record calls and loses no recorded fact", async () => {
  const before = await twoTaskRun("legacy");
  const after = await twoTaskRun("lean");
  // Exact counts: legacy = status 1 + inspection 2 + per task (bind 1, wave 1, directive+feedback 6, reviewer 1,
  // phase 1, integrate 2) x 2 + decision 1 + finish 1; lean drops inspection, waves and directive/feedback.
  const promptText = readFileSync(join(import.meta.dirname, "..", "prompts", "orchestrate.md"), "utf8");
  // What the current prompt requires: the derived total is asserted against the script and against the prompt.
  assert.match(promptText, /Do not record `interaction` directive or feedback events/);
  assert.match(promptText, /do not record optional phases such as `inspection`/);
  assert.match(promptText, /Declare the wave when more than one task will run concurrently/);
  for (const required of ['{"action":"status"}', "bind_workspace", "integrate_begin", "integrate_end"]) {
    assert.ok(promptText.includes(required), required);
  }
  assert.equal(before.leadRecordCalls, requiredLeadCalls({ inspectionPhase: true, singleTaskWave: true, directiveFeedback: true }));
  assert.equal(after.leadRecordCalls, requiredLeadCalls({ inspectionPhase: false, singleTaskWave: false, directiveFeedback: false }));
  assert.equal(before.leadRecordCalls, 29);
  assert.equal(after.leadRecordCalls, 13);
  for (const run of [before, after]) {
    assert.deepEqual(run.replayed.errors, []);
    assert.equal(run.replayed.final, "completed");
  }
  assert.deepEqual(facts(after.replayed.parent), facts(before.replayed.parent));
  const parent = after.replayed.parent;
  // Worker calls: task, step and route, plus a contract digest, for all six calls.
  const started = parent.activity.filter((entry: any) => entry.status === "started");
  assert.equal(started.length, 6);
  for (const entry of started) {
    assert.equal(entry.route, "CODEX_CLAUDE_CODEX");
    assert.match(entry.prompt_digest, /^[0-9a-f]{64}$/);
  }
  // Every legacy directive (a start) and feedback (a finish) has its activity entry in the lean record.
  for (const item of before.replayed.parent.interactions.filter((entry: any) => entry.type !== "decision")) {
    const status = item.type === "directive" ? "started" : "finished";
    assert.ok(parent.activity.some((entry: any) => entry.status === status && entry.task_id === item.task_id
      && entry.step_id === item.step_id), `${item.type} ${item.task_id}:${item.step_id}`);
  }
  assert.deepEqual(parent.interactions.map((entry: any) => entry.type), ["decision"]);
  // Auditability: every worker return keeps its meaning without a feedback event. Before this change the
  // activity entry held no result at all, so these assertions fail on the pre-PR behaviour.
  const returns = parent.activity.filter((entry: any) => entry.status === "finished");
  assert.equal(returns.length, 6);
  for (const entry of returns) {
    assert.ok(entry.result_summary.length > 0 && entry.result_summary.length <= 600);
    if (entry.tool === "spawn_subagent") assert.match(entry.result_summary, /committed at 244fea7; 80 passes, one skip/);
    assert.match(entry.result_digest, /^[0-9a-f]{64}$/);
  }
  assert.deepEqual(returns.filter((entry: any) => entry.tool === "spawn_subagent").map((entry: any) => entry.commit_sha),
    ["244fea7", "244fea7", "244fea7", "244fea7"]);
  for (const entry of started) assert.ok(entry.prompt_excerpt.length > 0 && entry.prompt_excerpt.length <= 300);
 
  assert.equal(parent.waves.length, 2);
  assert.ok(parent.waves.every((wave: any) => wave.id.startsWith("auto-") && wave.status === "integrated"));
});

test("the prompts no longer ask for directive, feedback, or optional phase events", () => {
  for (const file of ["orchestrate.md", "orchestrate-core.md"]) {
    const text = readFileSync(join(import.meta.dirname, "..", "prompts", file), "utf8");
    assert.doesNotMatch(text, /record a brief `interaction` directive/);
    assert.doesNotMatch(text, /"phase":"inspection","status":"in_progress"/);
    assert.match(text, /Do not record `interaction` directive or feedback events/);
  }
});

/**
 * Route tasks whose Codex specifier mutates and bind a worktree for each: only a mutating step starts an
 * implicit wave, so these tests need one. Task text names the worktree, as a mutating Codex contract must.
 */
async function routeMutating(h: ReturnType<typeof harness>, ids: string[], extra: { workerMutation?: "allowed" } = {}) {
  for (const id of ids) {
    await h.handlers.get("tool_result")!(routeFor(id, { specifierMutation: "allowed", ...extra }));
    await h.tools.get("orchestration_record").execute("b", { action: "bind_workspace", task_id: id,
      step_id: "codex_specifier", path: `/tmp/wt-${id}` });
  }
}
const wtTask = (label: string) => `work in /tmp/wt-${label.split(":")[0]}`;

test("gates still hold with implicit waves: concurrency, bind, bracket, and parent checkout", async () => {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "pico-implicit-gates-")));
  try {
    const h = harness({ ok: true, status: "READY" }, false, true, { executable: fakeClaude(dir) });
    await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: dir, ui: { notify() {} } });
    const record = h.tools.get("orchestration_record");
    await routeMutating(h, ["A", "B"], { workerMutation: "allowed" });
    const waves = () => h.calls.filter((call) => call.input?.kind === "wave");
    const spawn = (id: string, label: string) => ({ toolName: "spawn_subagent", toolCallId: id, input: { task: wtTask(label), label } });
    const done = (id: string) => h.handlers.get("tool_result")!({ toolName: "spawn_subagent", toolCallId: id, content: [],
      details: { subagent_id: `sub-${id}` } });
    // No integration of an unknown wave, and a mutating step is unbound until the lead binds it.
    assert.match((await record.execute("i", { action: "integrate_begin", wave_id: "auto" })).content[0].text, /WAVE_UNKNOWN/);
    const unbound = await h.handlers.get("tool_call")!({ toolName: "ClaudeWorker", toolCallId: "u",
      input: { prompt: "[pico-task A:claude_worker] go" } });
    assert.match(unbound.reason, /WORKER_WORKSPACE_UNBOUND/);
    assert.equal(waves().length, 0, "a blocked call records no wave");
    // One task alone: an implicit wave. A second task alongside it still needs declare_wave.
    assert.equal(await h.handlers.get("tool_call")!(spawn("a1", "A:codex_specifier")), undefined);
    assert.equal(waves().length, 1);
    assert.deepEqual([waves()[0].input!.task_ids, waves()[0].input!.status], [["A"], "declared"]);
    assert.match((await h.handlers.get("tool_call")!(spawn("b1", "B:codex_specifier"))).reason, /WAVE_UNDECLARED/);
    assert.match((await record.execute("w", { action: "declare_wave", wave_id: "w", tasks: [
      { task_id: "A", write_scope: ["a/**"] }, { task_id: "B", write_scope: ["b/**"] }] })).content[0].text, /WAVE_WORKERS_IN_FLIGHT/);
    // Another step of the same task adds no wave; the bracket is still required and still a barrier.
    await done("a1");
    assert.equal(await h.handlers.get("tool_call")!(spawn("a2", "A:codex_reviewer")), undefined);
    assert.equal(waves().length, 1);
    assert.match((await record.execute("i", { action: "integrate_begin", wave_id: "auto" })).content[0].text, /INTEGRATION_WORKERS_IN_FLIGHT/);
    await done("a2");
    assert.equal((await record.execute("i", { action: "integrate_begin", wave_id: "auto" })).details.ok, true);
    assert.match((await h.handlers.get("tool_call")!(spawn("a3", "A:codex_reviewer"))).reason, /INTEGRATION_IN_PROGRESS/);
    assert.equal((await record.execute("i", { action: "integrate_end", wave_id: "auto" })).details.ok, true);
    // A worker that writes the parent checkout still latches the violation inside an implicit wave.
    assert.equal(await h.handlers.get("tool_call")!(spawn("b2", "B:codex_specifier")), undefined);
    assert.equal(waves().length, 4, "declared, integrating, integrated for A, then B's own wave");
    h.checkout.digest = "dirty";
    h.checkout.status = ["M file"];
    const violated = await done("b2");
    assert.match(violated.content.at(-1).text, /WORKER_SCOPE_VIOLATION/);
    assert.match((await h.handlers.get("tool_call")!(spawn("b3", "B:codex_reviewer"))).reason, /WORKER_SCOPE_VIOLATION/);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("a refused implicit wave is diagnosed, records nothing, and never blocks the run", async () => {
  const h = harness({ ok: true, status: "READY" }, false, true,
    { waveEventResult: { ok: false, error_code: "PARENT_WAVE_TRANSITION_INVALID" } });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: "/tmp/target", ui: { notify() {} } });
  await routeMutating(h, ["A"]);
  const quiet = process.stderr.write;
  process.stderr.write = (() => true) as typeof process.stderr.write;
  try {
    for (const id of ["a1", "a2"]) {
      assert.equal(await h.handlers.get("tool_call")!({ toolName: "spawn_subagent", toolCallId: id,
        input: { task: wtTask("A"), label: "A:codex_specifier" } }), undefined);
      await h.handlers.get("tool_result")!({ toolName: "spawn_subagent", toolCallId: id, content: [], details: { subagent_id: id } });
    }
  } finally { process.stderr.write = quiet; }
  // Without a recorded wave nothing changes from before: the lead can still declare one and status is ready.
  assert.equal((await h.tools.get("orchestration_record").execute("s", { action: "status" })).details.ok, true);
  assert.equal((await h.tools.get("orchestration_record").execute("i", { action: "integrate_begin", wave_id: "auto" })).details.ok, false);
});

test("worker failure and integration conflict with auto-recorded events leave the run finishable", async () => {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "pico-implicit-failure-")));
  try {
    const h = harness({ ok: true, status: "READY" }, false, true, { executable: fakeClaude(dir) });
    await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: dir, ui: { notify() {} } });
    const record = h.tools.get("orchestration_record");
    await h.handlers.get("tool_result")!(routeFor("A", { workerMutation: "allowed" }));
    const workspace = join(dir, "wt");
    mkdirSync(workspace);
    await record.execute("b", { action: "bind_workspace", task_id: "A", step_id: "claude_worker", path: workspace });
    const failed = await claudeWorkerRun(h, "f", { prompt: "[pico-task A:claude_worker] FAIL" });
    assert.equal(failed.result!.isError, true);
    // No parentBlocked: the lead keeps recording, and a retry of the same step is allowed.
    assert.equal((await record.execute("s", { action: "status" })).details.ok, true);
    const retry = await claudeWorkerRun(h, "r", { prompt: "[pico-task A:claude_worker] go again" });
    assert.equal(retry.result!.isError, false);
    // Integration conflict: begin, a merge that cannot be resolved, so the lead never calls integrate_end.
    assert.equal((await record.execute("i", { action: "integrate_begin", wave_id: "auto" })).details.ok, true);
    assert.match((await h.handlers.get("tool_call")!({ toolName: "ClaudeWorker", toolCallId: "x",
      input: { prompt: "[pico-task A:claude_worker] more" } })).reason, /INTEGRATION_IN_PROGRESS/);
    await record.execute("p", { action: "event", event: { kind: "phase", phase: "A", status: "partial" } });
    assert.equal((await record.execute("f", { action: "finish", status: "PARTIAL" })).details.ok, true);
    const replayed = replay(eventsOf(h), "PARTIAL");
    // The retry's automatic "completed" replaces the earlier "failed"; the failure stays in worker_overrides.
    assert.deepEqual(replayed.errors, []);
    assert.equal(replayed.final, "partial");
    const parent = replayed.parent;
    assert.deepEqual(parent.activity.filter((entry: any) => entry.status === "failed").map((entry: any) => entry.step_id), ["claude_worker"]);
    const claude = parent.phases[0].workers.find((worker: any) => worker.step_id === "claude_worker");
    assert.deepEqual([claude.status, claude.blocker_code], ["completed", null]);
    assert.deepEqual(parent.worker_overrides.map((o: any) => [o.from_status, o.to_status, o.by, o.reason, o.from_blocker_code]),
      [["failed", "completed", "extension", "retry_succeeded", "MODEL_INVOCATION_FAILED"]]);
    // The abandoned integration is reconciled, not left open.
    assert.deepEqual(parent.waves.map((wave: any) => wave.status), ["abandoned"]);
    assert.deepEqual(parent.reconciled.filter((entry: any) => entry.kind === "wave").map((entry: any) => entry.from_status), ["integrating"]);
    const open = parent.phases.flatMap((phase: any) => [phase, ...phase.workers]).filter((item: any) => ["not_started", "in_progress"].includes(item.status));
    assert.deepEqual(open, []);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("resume after an interruption starts a fresh implicit wave without conflicting with the old one", async () => {
  const entries: any[] = [];
  const context = { cwd: process.cwd(), ui: { notify() {} },
    sessionManager: { getSessionId: () => "session-a", getBranch: () => entries } };
  const first = harness({ ok: true, status: "READY" }, false, true, { entries });
  await first.handlers.get("session_start")!({ reason: "startup" }, context);
  await first.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, context);
  await routeMutating(first, ["A"]);
  assert.equal(await first.handlers.get("tool_call")!({ toolName: "spawn_subagent", toolCallId: "a1",
    input: { task: wtTask("A"), label: "A:codex_specifier" } }), undefined);
  // The process dies with the worker in flight; the session is resumed in a new process.
  await first.handlers.get("session_shutdown")!({ reason: "reload" });
  const resumed = harness({ ok: true, status: "READY" }, false, true, { entries });
  await resumed.handlers.get("session_start")!({ reason: "startup" }, context);
  assert.equal((await resumed.tools.get("orchestration_record").execute("s", { action: "status" })).details.ok, true);
  await routeMutating(resumed, ["A"]);
  assert.equal(await resumed.handlers.get("tool_call")!({ toolName: "spawn_subagent", toolCallId: "a2",
    input: { task: wtTask("A"), label: "A:codex_specifier" } }), undefined);
  const wave = resumed.calls.find((call) => call.input?.kind === "wave");
  assert.deepEqual(wave?.input?.task_ids, ["A"]);
  assert.equal((await resumed.tools.get("orchestration_record").execute("s", { action: "status" })).details.ok, true);
  // Both processes' events replay cleanly into the real recorder; the second wave has its own id.
  const replayed = replay([...eventsOf(first), ...eventsOf(resumed)], null);
  assert.deepEqual(replayed.errors, []);
  assert.equal(replayed.parent.waves.length, 2);
  // The orphan of the interrupted process is superseded by the resumed wave and, with no COMPLETED, reconciled.
  assert.equal(replay([...eventsOf(first), ...eventsOf(resumed)], "PARTIAL").parent.waves.every((wave: any) => wave.status === "abandoned"), true);
});

const done = (h: ReturnType<typeof harness>, id: string, text = "ok") => h.handlers.get("tool_result")!({
  toolName: "spawn_subagent", toolCallId: id, content: [{ type: "text", text }], details: { subagent_id: `sub-${id}` } });
const spawnCall = (id: string, label: string) => ({ toolName: "spawn_subagent", toolCallId: id, input: { task: wtTask(label), label } });

test("implicit waves are one per task and never let tasks overlap; auto needs exactly one open", async () => {
  const h = harness({ ok: true, status: "READY" });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: "/tmp/target", ui: { notify() {} } });
  await routeMutating(h, ["A", "B"]);
  const record = h.tools.get("orchestration_record");
  // The reviewer's interleaving: specifier A, specifier B, then both reviewers at once.
  for (const [id, label] of [["a1", "A:codex_specifier"], ["b1", "B:codex_specifier"]]) {
    assert.equal(await h.handlers.get("tool_call")!(spawnCall(id, label)), undefined);
    await done(h, id);
  }
  assert.equal(await h.handlers.get("tool_call")!(spawnCall("a2", "A:codex_reviewer")), undefined);
  const overlap = await h.handlers.get("tool_call")!(spawnCall("b2", "B:codex_reviewer"));
  assert.match(overlap.reason, /WAVE_UNDECLARED/);
  await done(h, "a2");
  // Two implicit waves are open, one per task: auto names neither, the lead names one.
  const waves = h.calls.filter((call) => call.input?.kind === "wave");
  assert.deepEqual(waves.map((call) => call.input!.task_ids), [["A"], ["B"]]);
  const ambiguous = await record.execute("i", { action: "integrate_begin", wave_id: "auto" });
  assert.match(ambiguous.content[0].text, /WAVE_AUTO_AMBIGUOUS/);
  assert.equal((await record.execute("i", { action: "integrate_begin", wave_id: waves[0].input!.wave_id })).details.ok, true);
  assert.equal((await record.execute("i", { action: "integrate_end", wave_id: waves[0].input!.wave_id })).details.ok, true);
  // With one left, auto resolves to it.
  assert.equal((await record.execute("i", { action: "integrate_begin", wave_id: "auto" })).details.ok, true);
  assert.equal((await record.execute("i", { action: "integrate_end", wave_id: "auto" })).details.ok, true);
  const replayed = replay(eventsOf(h), null);
  assert.deepEqual(replayed.errors, []);
  assert.deepEqual(replayed.parent.waves.map((wave: any) => [wave.task_ids, wave.status]), [[["A"], "integrated"], [["B"], "integrated"]]);
  // Only an explicit wave allows overlap, after the write-scope and limit checks.
  assert.equal((await record.execute("w", { action: "declare_wave", wave_id: "w", tasks: [
    { task_id: "A", write_scope: ["x/**"] }, { task_id: "B", write_scope: ["x/y/**"] }] })).details.ok, false);
  assert.equal((await record.execute("w", { action: "declare_wave", wave_id: "w", tasks: [
    { task_id: "A", write_scope: ["a/**"] }, { task_id: "B", write_scope: ["b/**"] }] })).details.ok, true);
  assert.equal(await h.handlers.get("tool_call")!(spawnCall("a3", "A:codex_reviewer")), undefined);
  assert.equal(await h.handlers.get("tool_call")!(spawnCall("b3", "B:codex_reviewer")), undefined);
});

test("an unintegrated wave of a mutating task refuses COMPLETED; the contract goal and excerpt are recorded", async () => {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "pico-goal-")));
  try {
    const h = harness({ ok: true, status: "READY" }, false, true, { executable: fakeClaude(dir) });
    await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: dir, ui: { notify() {} } });
    const record = h.tools.get("orchestration_record");
    await h.handlers.get("tool_result")!(routeFor("A", { workerMutation: "allowed", specifierMutation: "allowed" }));
    const workspace = join(dir, "wt");
    mkdirSync(workspace);
    await record.execute("b", { action: "bind_workspace", task_id: "A", step_id: "claude_worker", path: workspace });
    await record.execute("b", { action: "bind_workspace", task_id: "A", step_id: "codex_specifier", path: workspace });
    const contract = `[pico-task A:codex_specifier]\nGoal: Find the race in the save path.\nContext: ${"boilerplate ".repeat(40)} ${workspace}`;
    assert.equal(await h.handlers.get("tool_call")!({ toolName: "spawn_subagent", toolCallId: "g1",
      input: { task: contract, label: "A:codex_specifier" } }), undefined);
    await done(h, "g1");
    for (const step of ["codex_specifier", "claude_worker", "codex_reviewer"]) {
      await record.execute("w", { action: "event", event: { kind: "worker", phase: "A", step_id: step, status: "completed",
        ...(step === "claude_worker" ? {} : { model: CODEX_HIGH }) } });
    }
    await record.execute("p", { action: "event", event: { kind: "phase", phase: "A", status: "completed", route: "CODEX_CLAUDE_CODEX" } });
    const refused = replay(eventsOf(h), "COMPLETED");
    assert.deepEqual(refused.errors.map((error) => error[1]), ["PARENT_WAVE_NOT_INTEGRATED"]);
    const started = replay(eventsOf(h), null).parent.activity.find((entry: any) => entry.status === "started");
    assert.equal(started.goal, "Find the race in the save path.");
    assert.match(started.prompt_excerpt, /^Context: boilerplate/);
    assert.doesNotMatch(started.prompt_excerpt, /pico-task|Goal:/);
    assert.ok(started.prompt_excerpt.length <= 300);
    // Integrated, it completes.
    await record.execute("i", { action: "integrate_begin", wave_id: "auto" });
    await record.execute("i", { action: "integrate_end", wave_id: "auto" });
    assert.deepEqual(replay(eventsOf(h), "COMPLETED").errors, []);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("a declared wave after an implicit one supersedes it; auto then refuses clearly", async () => {
  const h = harness({ ok: true, status: "READY" });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: "/tmp/target", ui: { notify() {} } });
  await routeMutating(h, ["A", "B", "C", "D"]);
  const record = h.tools.get("orchestration_record");
  assert.equal(await h.handlers.get("tool_call")!(spawnCall("a1", "A:codex_specifier")), undefined);
  await done(h, "a1");
  // Not integrated and not part of the next wave: it stays open and visible.
  assert.equal((await record.execute("w", { action: "declare_wave", wave_id: "w2", tasks: [
    { task_id: "B", write_scope: ["b/**"] }, { task_id: "C", write_scope: ["c/**"] }] })).details.ok, true);
  assert.equal((await record.execute("i", { action: "integrate_begin", wave_id: "w2" })).details.ok, true);
  assert.equal((await record.execute("i", { action: "integrate_end", wave_id: "w2" })).details.ok, true);
  // A new implicit wave while the earlier one is still open: two are open, so auto names none of them.
  assert.equal(await h.handlers.get("tool_call")!(spawnCall("d1", "D:codex_specifier")), undefined);
  await done(h, "d1");
  const ambiguous = await record.execute("i", { action: "integrate_begin", wave_id: "auto" });
  assert.equal(ambiguous.details.ok, false);
  assert.match(ambiguous.content[0].text, /WAVE_AUTO_AMBIGUOUS/);
  assert.equal(h.calls.filter((call) => call.input?.status === "integrating").length, 1, "a refused auto records nothing more");
  // The wave is still reachable by its id, and the dangling one is reconciled when the run is not COMPLETED.
  const dId = h.calls.filter((call) => call.input?.kind === "wave").at(-1)!.input!.wave_id as string;
  assert.equal((await record.execute("i", { action: "integrate_begin", wave_id: dId })).details.ok, true);
  assert.equal((await record.execute("i", { action: "integrate_end", wave_id: dId })).details.ok, true);
  const replayed = replay(eventsOf(h), "PARTIAL");
  assert.deepEqual(replayed.errors, []);
  assert.deepEqual(replayed.parent.waves.map((wave: any) => wave.status), ["abandoned", "integrated", "integrated"]);
  assert.deepEqual(replayed.parent.reconciled.filter((entry: any) => entry.kind === "wave").map((entry: any) => entry.from_status), ["declared"]);
});

test("a read-only reviewer after integration adds no wave: one integrated wave, and the phase points at it", async () => {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "pico-no-stray-wave-")));
  try {
    const h = harness({ ok: true, status: "READY" }, false, true, { executable: fakeClaude(dir) });
    await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: dir, ui: { notify() {} } });
    const record = h.tools.get("orchestration_record");
    await h.handlers.get("tool_result")!(routeFor("A", { workerMutation: "allowed" }));
    const workspace = join(dir, "wt");
    mkdirSync(workspace);
    await record.execute("b", { action: "bind_workspace", task_id: "A", step_id: "claude_worker", path: workspace });
    // A read-only specifier first: no wave. The mutating Claude worker starts the task's one wave.
    assert.equal(await h.handlers.get("tool_call")!(spawnCall("s1", "A:codex_specifier")), undefined);
    await done(h, "s1");
    assert.equal(h.calls.filter((call) => call.input?.kind === "wave").length, 0);
    assert.equal((await claudeWorkerRun(h, "c1", { prompt: "[pico-task A:claude_worker] implement" })).guard, undefined);
    assert.equal((await record.execute("i", { action: "integrate_begin", wave_id: "auto" })).details.ok, true);
    assert.equal((await record.execute("i", { action: "integrate_end", wave_id: "auto" })).details.ok, true);
    // The read-only reviewer after integration: still one wave.
    assert.equal(await h.handlers.get("tool_call")!(spawnCall("r1", "A:codex_reviewer")), undefined);
    await done(h, "r1");
    const replayed = replay(eventsOf(h), null);
    assert.deepEqual(replayed.errors, []);
    const { waves, phases } = replayed.parent;
    assert.deepEqual(waves.map((wave: any) => [wave.task_ids, wave.status]), [[["A"], "integrated"]]);
    assert.equal(phases[0].wave_id, waves[0].id);
    // The task mutating again after integration starts a new wave.
    assert.equal((await claudeWorkerRun(h, "c2", { prompt: "[pico-task A:claude_worker] repair" })).guard, undefined);
    assert.equal(replay(eventsOf(h), null).parent.waves.length, 2);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("a Codex step with no lead worker event still records its model, effort and terminal status", async () => {
  const h = harness({ ok: true, status: "READY" });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: "/tmp/target", ui: { notify() {} } });
  await h.handlers.get("tool_result")!(routeFor("A"));
  const run = async (id: string, step: string, fail = false) => {
    assert.equal(await h.handlers.get("tool_call")!({ toolName: "spawn_subagent", toolCallId: id,
      input: { task: "review", label: `A:${step}` } }), undefined);
    await h.handlers.get("tool_result")!({ toolName: "spawn_subagent", toolCallId: id, isError: fail,
      content: [{ type: "text", text: "done" }], details: { subagent_id: `s-${id}`, elapsedMs: 3 } });
  };
  await run("r1", "codex_reviewer");
  await run("s1", "codex_specifier", true);
  const replayed = replay(eventsOf(h), null);
  assert.deepEqual(replayed.errors, []);
  const workers = Object.fromEntries(replayed.parent.phases[0].workers.map((worker: any) => [worker.step_id, worker]));
  assert.deepEqual([workers.codex_reviewer.model, workers.codex_reviewer.reasoning_effort, workers.codex_reviewer.status],
    [CODEX_HIGH, "high", "completed"]);
  assert.deepEqual([workers.codex_specifier.status, workers.codex_specifier.blocker_code], ["failed", "WORKER_FAILED"]);
  // The lead's judgment still wins: it restates the model and overrides the completed worker.
  const lead = { kind: "worker", phase: "A", step_id: "codex_reviewer", status: "failed", blocker_code: "REVIEW_REJECTED", model: CODEX_HIGH };
  const judged = replay([...eventsOf(h), lead], null);
  assert.deepEqual(judged.errors, []);
  assert.equal(judged.parent.phases[0].workers.find((worker: any) => worker.step_id === "codex_reviewer").status, "failed");
});

test("the lead restating an auto-recorded Codex worker the old way is accepted and changes nothing", async () => {
  const h = harness({ ok: true, status: "READY" });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: "/tmp/target", ui: { notify() {} } });
  await h.handlers.get("tool_result")!(routeFor("A"));
  assert.equal(await h.handlers.get("tool_call")!({ toolName: "spawn_subagent", toolCallId: "r1",
    input: { task: "review", label: "A:codex_reviewer" } }), undefined);
  await h.handlers.get("tool_result")!({ toolName: "spawn_subagent", toolCallId: "r1", content: [{ type: "text", text: "ok" }],
    details: { subagent_id: "s-r1", elapsedMs: 3 } });
  const reviewer = { kind: "worker", phase: "A", step_id: "codex_reviewer" };
  const habits = [
    { ...reviewer, status: "completed", model: "gpt-6.1-sol" },                       // provider prefix dropped
    { ...reviewer, status: "completed", model: CODEX_HIGH, reasoning_effort: "low" },  // plan said high
    { ...reviewer, status: "in_progress", model: CODEX_HIGH },                         // start after the automatic completed
    { ...reviewer, status: "completed", model: CODEX_HIGH, reasoning_effort: "high" },
  ];
  const replayed = replay([...eventsOf(h), ...habits], null);
  assert.deepEqual(replayed.errors, []);
  const worker = replayed.parent.phases[0].workers.find((item: any) => item.step_id === "codex_reviewer");
  assert.deepEqual([worker.status, worker.model, worker.reasoning_effort, worker.auto_recorded], ["completed", CODEX_HIGH, "high", true]);
  assert.equal(replayed.parent.worker_overrides, undefined, "a restatement is not an override");
  // A real difference of judgment is still recorded, and a worker the lead alone recorded still conflicts.
  const judged = replay([...eventsOf(h), { ...reviewer, status: "failed", blocker_code: "REVIEW_REJECTED" },
    { kind: "worker", phase: "A", step_id: "lead_only", status: "completed", model: "m/one" },
    { kind: "worker", phase: "A", step_id: "lead_only", status: "completed", model: "m/two" }], null);
  assert.deepEqual(judged.errors.map((error) => error[1]), ["PARENT_EVENT_CONFLICT"]);
  assert.equal(judged.parent.phases[0].workers.find((item: any) => item.step_id === "codex_reviewer").status, "failed");
});

test("a refused restatement of an extension-recorded worker never blocks; one of the lead's own workers still does", async () => {
  const h = harness({ ok: true, status: "READY" }, false, true,
    { workerRestatementResult: { ok: false, error_code: "PARENT_EVENT_CONFLICT" } });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: "/tmp/target", ui: { notify() {} } });
  await h.handlers.get("tool_result")!(routeFor("A"));
  const record = h.tools.get("orchestration_record");
  assert.equal(await h.handlers.get("tool_call")!({ toolName: "spawn_subagent", toolCallId: "r1",
    input: { task: "review", label: "A:codex_reviewer" } }), undefined);
  await h.handlers.get("tool_result")!({ toolName: "spawn_subagent", toolCallId: "r1", content: [], details: { subagent_id: "s-r1" } });
  const quiet = process.stderr.write;
  process.stderr.write = (() => true) as typeof process.stderr.write;
  try {
    await record.execute("w", { action: "event", event: { kind: "worker", phase: "A", step_id: "codex_reviewer", status: "completed", model: "other/model" } });
    assert.equal(await h.handlers.get("tool_call")!({ toolName: "spawn_subagent", toolCallId: "r2",
      input: { task: "review", label: "A:codex_reviewer" } }), undefined, "the run is not blocked");
    await record.execute("w", { action: "event", event: { kind: "worker", phase: "A", step_id: "lead_only", status: "completed", model: "m/two" } });
    const blocked = await h.handlers.get("tool_call")!({ toolName: "spawn_subagent", toolCallId: "r3",
      input: { task: "review", label: "A:codex_reviewer" } });
    assert.match(blocked.reason, /provenance is blocked/);
  } finally { process.stderr.write = quiet; }
});

test("work after an integration with its wave write refused still refuses COMPLETED", async () => {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "pico-regress-")));
  try {
    const h = harness({ ok: true, status: "READY" }, false, true, { executable: fakeClaude(dir) });
    await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: dir, ui: { notify() {} } });
    const record = h.tools.get("orchestration_record");
    await h.handlers.get("tool_result")!(routeFor("A", { workerMutation: "allowed" }));
    const workspace = join(dir, "wt");
    mkdirSync(workspace);
    await record.execute("b", { action: "bind_workspace", task_id: "A", step_id: "claude_worker", path: workspace });
    assert.equal((await claudeWorkerRun(h, "c1", { prompt: "[pico-task A:claude_worker] implement" })).guard, undefined);
    await record.execute("i", { action: "integrate_begin", wave_id: "auto" });
    await record.execute("i", { action: "integrate_end", wave_id: "auto" });
    // The same bound worktree mutates again; the recorder refuses that second implicit-wave write.
    assert.equal((await claudeWorkerRun(h, "c2", { prompt: "[pico-task A:claude_worker] repair" })).guard, undefined);
    const waveDeclarations = eventsOf(h).filter((event: any) => event.kind === "wave" && event.status === "declared");
    assert.equal(waveDeclarations.length, 2);
    const events = eventsOf(h).filter((event) => event !== waveDeclarations[1]);
    for (const step of ["codex_specifier", "codex_reviewer"]) {
      events.push({ kind: "worker", phase: "A", step_id: step, status: "completed", model: CODEX_HIGH });
    }
    events.push({ kind: "phase", phase: "A", status: "completed", route: "CODEX_CLAUDE_CODEX" });
    const refused = replay(events, "COMPLETED");
    assert.deepEqual(refused.errors.map((error) => error[1]), ["PARENT_WAVE_NOT_INTEGRATED"]);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("a lead event never carries the extension's automatic marks to the recorder", async () => {
  const h = harness({ ok: true, status: "READY" });
  await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: "/tmp/target", ui: { notify() {} } });
  await h.tools.get("orchestration_record").execute("w", { action: "event", event: {
    kind: "worker", phase: "A", step_id: "codex_reviewer", status: "completed", only_if_open: true, auto_recorded: true } });
  const forwarded = h.parentCalls("event").map((call) => call.input!).filter((input) => input.kind === "worker");
  assert.deepEqual(forwarded, [{ kind: "worker", phase: "A", step_id: "codex_reviewer", status: "completed" }]);
});

// --- lead-call limit (LEAD_CALL_LIMIT) ---
async function withLeadLimit(env: Record<string, string | undefined>, options: Parameters<typeof harness>[3],
                             body: (h: ReturnType<typeof harness>, aborts: number[]) => Promise<void>) {
  const names = ["PI_ORCHESTRATION_MAX_LEAD_CALLS", "PI_ORCHESTRATION_MAX_LEAD_CALLS_SOURCE", "PI_ORCHESTRATION_LEAD_CALLS_WARN_AT"];
  const saved = names.map((name) => process.env[name]);
  for (const name of names) delete process.env[name];
  for (const [name, value] of Object.entries(env)) if (value !== undefined) process.env[name] = value;
  try {
    const h = harness({ ok: true, status: "READY" }, false, true, options);
    const aborts: number[] = [];
    await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" },
      { cwd: "/tmp/target", ui: { notify() {} }, abort: () => { aborts.push(1); } });
    await body(h, aborts);
  } finally {
    names.forEach((name, index) => { if (saved[index] === undefined) delete process.env[name]; else process.env[name] = saved[index]; });
  }
}
let leadClock = 0;
const leadCall = (h: ReturnType<typeof harness>, content: unknown[] = []) => h.handlers.get("message_end")!({ message: {
  role: "assistant", provider: "openai-codex", model: "gpt-6.1-sol", timestamp: ++leadClock, content,
  usage: { input: 10, output: 2, totalTokens: 12 } } });
const finishCall = [{ type: "toolCall", name: "orchestration_record", arguments: { action: "finish", status: "COMPLETED" } }];
const limitEvents = (h: ReturnType<typeof harness>) => h.parentCalls("event").map((call) => call.input!).filter((input) =>
  ["lead_limit", "lead_call_warning", "lead_call_limit_reached"].includes(String(input.kind)));
const bash = { toolName: "bash", input: { command: "ls" } };

test("no lead-call limit: nothing is recorded and tools stay available however many calls are made", async () => {
  await withLeadLimit({}, {}, async (h) => {
    for (let i = 0; i < 40; i++) await leadCall(h);
    assert.equal(await h.handlers.get("tool_call")!(bash), undefined);
    assert.deepEqual(limitEvents(h), []);
    assert.equal(h.messages.length, 0);
    assert.equal(h.parentCalls("finish").length, 0);
  });
});

test("the warning fires once at 80% by default (rounded down) and blocks nothing", async () => {
  await withLeadLimit({ PI_ORCHESTRATION_MAX_LEAD_CALLS: "5", PI_ORCHESTRATION_MAX_LEAD_CALLS_SOURCE: "task" }, {}, async (h) => {
    assert.deepEqual(limitEvents(h), [{ kind: "lead_limit", state: "set", max_lead_calls: 5, source: "task", warn_at: 4 }]);
    for (let i = 0; i < 3; i++) await leadCall(h);
    assert.equal(h.messages.length, 0);
    await leadCall(h);
    assert.equal(h.messages.length, 1);
    assert.equal(h.messages[0].message.customType, "lead-call-warning");
    assert.match(h.messages[0].message.content, /4 of 5 lead calls.*Converge/s);
    assert.equal(await h.handlers.get("tool_call")!(bash), undefined, "the warning blocks nothing");
    await leadCall(h);
    assert.equal(h.messages.length, 1, "warned once");
    assert.equal(limitEvents(h).filter((event) => event.kind === "lead_call_warning").length, 1);
  });
});

test("the warning threshold is configurable, must be below the limit, and a limit of 1 has no default warning", async () => {
  await withLeadLimit({ PI_ORCHESTRATION_MAX_LEAD_CALLS: "10", PI_ORCHESTRATION_LEAD_CALLS_WARN_AT: "3" }, {}, async (h) => {
    assert.equal(limitEvents(h)[0].warn_at, 3);
    await leadCall(h); await leadCall(h);
    assert.equal(h.messages.length, 0);
    await leadCall(h);
    assert.equal(h.messages.length, 1);
  });
  await withLeadLimit({ PI_ORCHESTRATION_MAX_LEAD_CALLS: "1" }, {}, async (h) => {
    assert.equal("warn_at" in limitEvents(h)[0], false);
  });
  await withLeadLimit({ PI_ORCHESTRATION_MAX_LEAD_CALLS: "4", PI_ORCHESTRATION_LEAD_CALLS_WARN_AT: "4" }, {}, async (h) => {
    assert.deepEqual(limitEvents(h), []);
    assert.equal((await h.handlers.get("tool_call")!(bash)).block, true);
  });
  await withLeadLimit({ PI_ORCHESTRATION_MAX_LEAD_CALLS: "10" }, { limits: { ok: true, max_lead_calls: 10, lead_calls_warn_at: 6 } }, async (h) => {
    assert.equal(limitEvents(h)[0].warn_at, 6);
  });
});

test("reaching the limit blocks tools except status and finish; the lead's finish within the grace is honoured once", async () => {
  await withLeadLimit({ PI_ORCHESTRATION_MAX_LEAD_CALLS: "3" }, {}, async (h, aborts) => {
    await leadCall(h); await leadCall(h);
    assert.equal(await h.handlers.get("tool_call")!(bash), undefined);
    await leadCall(h);
    const blocked = await h.handlers.get("tool_call")!(bash);
    assert.equal(blocked.block, true);
    assert.match(blocked.reason, /^LEAD_CALL_LIMIT: 3 of 3 lead calls are used/);
    assert.match(blocked.reason, /2 more model call\(s\)/);
    assert.equal((await h.handlers.get("tool_call")!({ toolName: "route_task", input: {} })).block, true);
    assert.equal((await h.handlers.get("tool_call")!({ toolName: "orchestration_record", input: { action: "event" } })).block, true);
    assert.equal(await h.handlers.get("tool_call")!({ toolName: "orchestration_record", input: { action: "status" } }), undefined);
    const status = await h.tools.get("orchestration_record").execute("s", { action: "status" });
    assert.match(status.content[0].text, /Lead calls: 3 of 3 \(limit reached: finish now\)/);
    await leadCall(h);
    await leadCall(h, finishCall);   // the last grace slot carries the finish
    assert.equal(await h.handlers.get("tool_call")!({ toolName: "orchestration_record", input: { action: "finish" } }), undefined);
    const done = await h.tools.get("orchestration_record").execute("f", { action: "finish", status: "PARTIAL", blocker_code: "OUT_OF_CALLS" });
    assert.equal(done.details.ok, true);
    await leadCall(h);               // the run is finished: nothing more is counted or enforced
    const finishes = h.parentCalls("finish");
    assert.equal(finishes.length, 1);
    assert.equal(finishes[0].args[2], "PARTIAL");
    assert.equal(finishes[0].args.at(-1), "OUT_OF_CALLS");
    assert.deepEqual(aborts, []);
    assert.equal(h.parentCalls("event").some((call) => call.input?.kind === "lead_call_limit_reached"), false);
    assert.equal(h.leaseCalls("release").length, 0);
  });
});

test("grace exhausted: the run is stopped IN_PROGRESS with a checkpoint event, the lease released by its owner, the agent aborted", async () => {
  await withLeadLimit({ PI_ORCHESTRATION_MAX_LEAD_CALLS: "2", PI_ORCHESTRATION_MAX_LEAD_CALLS_SOURCE: "manifest" }, {}, async (h, aborts) => {
    await leadCall(h); await leadCall(h); await leadCall(h);
    assert.equal(h.parentCalls("event").some((call) => call.input?.kind === "lead_call_limit_reached"), false);
    await leadCall(h);               // calls == limit + grace and no finish in it
    assert.equal(h.parentCalls("finish").length, 0, "the run is never finished by the limit");
    assert.equal(h.parentCalls("settle").length, 0);
    const kinds = limitEvents(h).map((event) => event.kind === "lead_limit" ? `lead_limit:${event.state}` : event.kind);
    assert.deepEqual(kinds, ["lead_limit:set", "lead_call_warning", "lead_limit:reached", "lead_call_limit_reached"]);
    const releases = h.leaseCalls("release");
    assert.equal(releases.length, 1);
    assert.deepEqual(releases[0].args.slice(0, 2), ["release", "parent-test"]);
    assert.ok(releases[0].args.includes("--owner"), "released through the owner path");
    // The stop is recorded (and the checkpoint written) before the lease is released.
    const order = h.calls.map((call) => call.input?.kind === "lead_call_limit_reached" ? "stop" : call.args[0]);
    assert.ok(order.indexOf("stop") < order.indexOf("release"));
    assert.deepEqual(aborts, [1]);
    const write = await h.handlers.get("tool_call")!({ toolName: "orchestration_record", input: { action: "finish" } });
    assert.equal(write.block, true);
    assert.equal(write.terminate, true);
    assert.equal((await h.handlers.get("tool_call")!(bash)).block, true);
    const status = await h.tools.get("orchestration_record").execute("st", { action: "status" });
    assert.match(status.content[0].text, /left in progress/);
    assert.equal(status.details.active, false);
    // The aborted agent and the shutdown that follows must not settle (finish) the run.
    await h.handlers.get("agent_before_settle")!({ outcome: "aborted", continue: false });
    await h.handlers.get("session_shutdown")!({ reason: "quit" });
    await leadCall(h);
    assert.equal(h.parentCalls("finish").length, 0);
    assert.equal(h.parentCalls("settle").length, 0);
    assert.equal(h.leaseCalls("release").length, 1);
    assert.equal(h.entries.some((entry: any) => entry.data?.active === false), false, "the session marker stays active for the resume");
  });
});

test("workers still in flight at the stop are recorded blocked and the stop leaves no worker aborted silently", async () => {
  await withLeadLimit({ PI_ORCHESTRATION_MAX_LEAD_CALLS: "1" }, {}, async (h, aborts) => {
    await h.handlers.get("tool_result")!(routed);
    await h.handlers.get("tool_call")!({ toolName: "spawn_subagent", toolCallId: "s1", input: { task: "x" } },
      { cwd: "/tmp/target", ui: { notify() {} }, abort: () => { aborts.push(1); } });
    await leadCall(h); await leadCall(h); await leadCall(h);
    const stopped = h.calls.findIndex((call) => call.input?.kind === "lead_call_limit_reached");
    assert.ok(stopped >= 0);
    const blocked = h.calls.slice(0, stopped).filter((call) => call.input?.kind === "worker" && call.input.blocker_code === "LEAD_CALL_LIMIT");
    assert.ok(blocked.length >= 1, "the in-flight worker is recorded before the stop");
    assert.equal(blocked[0].input?.status, "blocked");
    assert.deepEqual(aborts, [1]);
  });
});

test("a finish that fails in the last grace call is still followed by the stop on the next call", async () => {
  await withLeadLimit({ PI_ORCHESTRATION_MAX_LEAD_CALLS: "1" }, { completionError: "PARENT_COMPLETION_INCOMPLETE" }, async (h, aborts) => {
    await leadCall(h); await leadCall(h);
    await leadCall(h, finishCall);
    assert.equal(h.parentCalls("event").some((call) => call.input?.kind === "lead_call_limit_reached"), false);
    const refused = await h.tools.get("orchestration_record").execute("f", { action: "finish", status: "COMPLETED" });
    assert.equal(refused.details.ok, false);
    await leadCall(h);
    assert.equal(h.parentCalls("event").filter((call) => call.input?.kind === "lead_call_limit_reached").length, 1);
    assert.deepEqual(aborts, [1]);
  });
});

test("a resumed run keeps counting from the recorded calls and warns at once when already past the threshold", async () => {
  const earlier = Array.from({ length: 7 }, (_, index) => ({ sequence: index + 1 }));
  await withLeadLimit({ PI_ORCHESTRATION_MAX_LEAD_CALLS: "9", PI_ORCHESTRATION_MAX_LEAD_CALLS_SOURCE: "cli" },
    { eventResult: { ok: true, context: { lead_calls: earlier } } }, async (h) => {
      assert.equal(h.messages.length, 1, "7 recorded calls are past the default threshold of 7 (80% of 9)");
      assert.equal(await h.handlers.get("tool_call")!(bash), undefined);
      await leadCall(h);
      assert.equal(await h.handlers.get("tool_call")!(bash), undefined);
      await leadCall(h);
      assert.equal((await h.handlers.get("tool_call")!(bash)).block, true, "7 + 2 new calls reach the limit of 9");
    });
});

test("the repository policy supplies the limit; an explicit one wins and an invalid one blocks the run", async () => {
  await withLeadLimit({}, { limits: { ok: true, parallel_tasks: 3, open_prs: 2, max_lead_calls: 4 } }, async (h) => {
    assert.deepEqual(limitEvents(h), [{ kind: "lead_limit", state: "set", max_lead_calls: 4, source: "policy", warn_at: 3 }]);
  });
  await withLeadLimit({ PI_ORCHESTRATION_MAX_LEAD_CALLS: "9" }, { limits: { ok: true, max_lead_calls: 4 } }, async (h) => {
    assert.deepEqual(limitEvents(h), [{ kind: "lead_limit", state: "set", max_lead_calls: 9, source: "env", warn_at: 7 }]);
  });
  for (const bad of ["0", "-1", "2.5", "abc"]) {
    await withLeadLimit({ PI_ORCHESTRATION_MAX_LEAD_CALLS: bad }, {}, async (h) => {
      assert.deepEqual(limitEvents(h), []);
      assert.equal((await h.handlers.get("tool_call")!(bash)).block, true);
    });
  }
});

test("the lead cannot forge a lead-call limit event", () => {
  const h = harness({ ok: true, status: "READY" });
  const params = h.tools.get("orchestration_record").parameters;
  for (const kind of ["lead_limit", "lead_call_warning", "lead_call_limit_reached"]) {
    assert.equal(Value.Check(params, { action: "event", event: { kind, state: "set" } }), false);
  }
});

test("a restored (resumed) session is bound by the limit and counts from the larger of telemetry and its own count", async () => {
  const names = ["PI_ORCHESTRATION_MAX_LEAD_CALLS", "PI_ORCHESTRATION_MAX_LEAD_CALLS_SOURCE", "PI_ORCHESTRATION_LEAD_CALLS_WARN_AT"];
  const saved = names.map((name) => process.env[name]);
  for (const name of names) delete process.env[name];
  process.env.PI_ORCHESTRATION_MAX_LEAD_CALLS = "5";
  process.env.PI_ORCHESTRATION_MAX_LEAD_CALLS_SOURCE = "cli";
  try {
    const cwd = process.cwd();
    const entries: any[] = [{ type: "custom", customType: "pi-orchestration-parent-v1",
      data: { run_id: "parent-test", session_id: "session-a", target_cwd: cwd, active: true } }];
    // 2 telemetry rows were lost: the extension's own count says 3, the recorded rows say 1.
    const aborts: number[] = [];
    const h = harness({ ok: true, status: "READY" }, false, true, { entries,
      eventResult: { ok: true, context: { lead_calls: [{}], lead_call_limit: { calls_seen: 3 } } } });
    await h.handlers.get("session_start")!({ reason: "startup" }, { cwd, abort: () => { aborts.push(1); },
      ui: { notify() {} }, sessionManager: { getSessionId: () => "session-a", getBranch: () => entries } });
    assert.equal(h.parentCalls("resume").length, 1);
    assert.deepEqual(limitEvents(h)[0], { kind: "lead_limit", state: "set", max_lead_calls: 5, source: "cli", warn_at: 4 });
    assert.equal(await h.handlers.get("tool_call")!(bash, { cwd, abort: () => { aborts.push(1); }, ui: { notify() {} } }), undefined);
    await leadCall(h);                       // 4: warning
    assert.equal(h.messages.length, 1);
    await leadCall(h);                       // 5: limit reached
    const blocked = await h.handlers.get("tool_call")!(bash, { cwd, abort: () => { aborts.push(1); }, ui: { notify() {} } });
    assert.equal(blocked.block, true);
    assert.match(blocked.reason, /^LEAD_CALL_LIMIT: 5 of 5/);
    await leadCall(h); await leadCall(h);    // grace used up
    const stop = h.parentCalls("event").map((call) => call.input!).find((input) => input.kind === "lead_call_limit_reached");
    assert.deepEqual(stop, { kind: "lead_call_limit_reached", calls: 7 });
    assert.deepEqual(aborts, [1]);
    assert.equal(h.parentCalls("finish").length, 0);
  } finally {
    names.forEach((name, index) => { if (saved[index] === undefined) delete process.env[name]; else process.env[name] = saved[index]; });
  }
});

test("a call whose model string fails validation is still counted against the limit", async () => {
  await withLeadLimit({ PI_ORCHESTRATION_MAX_LEAD_CALLS: "1" }, {}, async (h) => {
    await h.handlers.get("message_end")!({ message: { role: "assistant", provider: "bad provider!", model: "x", timestamp: ++leadClock, content: [] } });
    assert.equal(h.parentCalls("event").some((call) => call.input?.kind === "lead_usage"), false);
    assert.equal((await h.handlers.get("tool_call")!(bash)).block, true);
    assert.deepEqual(limitEvents(h).at(-1), { kind: "lead_limit", state: "reached", calls: 1 });
  });
});

test("when the stop checkpoint cannot be written the lease is kept, the agent is aborted and settle ends the run BLOCKED", async () => {
  await withLeadLimit({ PI_ORCHESTRATION_MAX_LEAD_CALLS: "1" }, { failKind: "lead_call_limit_reached" }, async (h, aborts) => {
    await leadCall(h); await leadCall(h); await leadCall(h);
    assert.equal(h.leaseCalls("release").length, 0, "the lease is not released without a checkpoint");
    assert.deepEqual(aborts, [1]);
    assert.equal((await h.handlers.get("tool_call")!(bash)).block, true);
    await h.handlers.get("agent_before_settle")!({ outcome: "aborted", continue: false });
    assert.equal(h.parentCalls("settle").length, 1, "the normal settle path ends the run");
    assert.equal(h.parentCalls("finish").length, 0);
  });
});

// --- repeated ineffective actions (REPEATED_INEFFECTIVE_ACTION) ---
async function withFailureBudget(env: Record<string, string | undefined>, options: Parameters<typeof harness>[3],
                                 body: (h: ReturnType<typeof harness>) => Promise<void>) {
  const names = ["PI_ORCHESTRATION_FAILURE_RETRY_BUDGET", "PI_ORCHESTRATION_FAILURE_RETRY_BUDGET_SOURCE", "PI_ORCHESTRATION_INPUTS_DIGEST"];
  const saved = names.map((name) => process.env[name]);
  for (const name of names) delete process.env[name];
  for (const [name, value] of Object.entries(env)) if (value !== undefined) process.env[name] = value;
  try {
    const h = harness({ ok: true, status: "READY" }, false, true, options);
    await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: "/tmp/target", ui: { notify() {} } });
    await body(h);
  } finally {
    names.forEach((name, index) => { if (saved[index] === undefined) delete process.env[name]; else process.env[name] = saved[index]; });
  }
}
const codexStep = async (h: ReturnType<typeof harness>, id: string, label: string,
                         outcome: { fail?: boolean; details?: Record<string, unknown> } = {}) => {
  const guard = await h.handlers.get("tool_call")!({ toolName: "spawn_subagent", toolCallId: id, input: { task: "go", label } });
  if (guard) return { guard };
  const after = await h.handlers.get("tool_result")!({ toolName: "spawn_subagent", toolCallId: id, isError: outcome.fail === true,
    content: [{ type: "text", text: "done" }], details: { subagent_id: `s-${id}`, elapsedMs: 3, ...outcome.details } });
  return { guard, after };
};
const eventsOfKind = (h: ReturnType<typeof harness>, kind: string) => eventsOf(h).filter((input: any) => input.kind === kind);

test("the retry budget comes from the launcher, else the repository policy, else the default of 2", async () => {
  const gateBudget = async (env: Record<string, string | undefined>, options: Parameters<typeof harness>[3] = {}) => {
    let seen: Record<string, unknown> | undefined;
    await withFailureBudget(env, options, async (h) => {
      await h.handlers.get("tool_result")!(routeFor("A"));
      await codexStep(h, "r1", "A:codex_reviewer");
      seen = eventsOfKind(h, "retry_gate")[0] as Record<string, unknown>;
    });
    return seen;
  };
  const base = { kind: "retry_gate", phase: "A", step_id: "codex_reviewer", head: "a".repeat(40) };
  assert.deepEqual(await gateBudget({}), { ...base, retry_budget: 2, source: "default" });
  assert.deepEqual(await gateBudget({}, { limits: { ok: true, parallel_tasks: 3, failure_retry_budget: 0 } }),
    { ...base, retry_budget: 0, source: "policy" }, "0 is a real policy value");
  assert.deepEqual(await gateBudget({ PI_ORCHESTRATION_FAILURE_RETRY_BUDGET: "5", PI_ORCHESTRATION_FAILURE_RETRY_BUDGET_SOURCE: "task" },
    { limits: { ok: true, parallel_tasks: 3, failure_retry_budget: 9 } }), { ...base, retry_budget: 5, source: "task" });
  assert.deepEqual(await gateBudget({ PI_ORCHESTRATION_FAILURE_RETRY_BUDGET: "0" }), { ...base, retry_budget: 0, source: "env" });
  const digest = "d".repeat(64);
  assert.equal(((await gateBudget({ PI_ORCHESTRATION_INPUTS_DIGEST: digest })) as any).inputs_digest, digest);
  assert.equal(((await gateBudget({ PI_ORCHESTRATION_INPUTS_DIGEST: "not-a-digest" })) as any).inputs_digest, undefined);
});

test("an invalid retry budget (environment or policy) blocks the run closed", async () => {
  for (const [env, options] of [
    [{ PI_ORCHESTRATION_FAILURE_RETRY_BUDGET: "-1" }, {}], [{ PI_ORCHESTRATION_FAILURE_RETRY_BUDGET: "two" }, {}],
    [{ PI_ORCHESTRATION_FAILURE_RETRY_BUDGET: "10000" }, {}],
    [{}, { limits: { ok: true, parallel_tasks: 3, failure_retry_budget: -2 } }],
    [{}, { limits: { ok: true, parallel_tasks: 3, failure_retry_budget: 1.5 } }],
  ] as [Record<string, string>, Parameters<typeof harness>[3]][]) {
    await withFailureBudget(env, options, async (h) => {
      const blocked = await h.handlers.get("tool_call")!({ toolName: "bash", toolCallId: "b", input: { command: "ls" } });
      assert.equal(blocked.block, true);
    });
  }
});

test("a failed worker records a structured failure with its code, HEAD and inputs digest; an abort and a success record none", async () => {
  const digest = "e".repeat(64);
  await withFailureBudget({ PI_ORCHESTRATION_INPUTS_DIGEST: digest }, {}, async (h) => {
    await h.handlers.get("tool_result")!(routeFor("A"));
    await codexStep(h, "ok", "A:codex_reviewer");
    assert.deepEqual(eventsOfKind(h, "failure"), []);
    await codexStep(h, "bad", "A:codex_reviewer", { fail: true, details: { blocker_code: "TESTS_FAILED" } });
    await codexStep(h, "plain", "A:codex_specifier", { fail: true });
    await codexStep(h, "abort", "A:codex_specifier", { fail: true, details: { error: "ABORTED" } });
    await codexStep(h, "ignored", "A:codex_specifier", { fail: true, details: { blocker_code: "lower case text" } });
    assert.deepEqual(eventsOfKind(h, "failure"), [
      { kind: "failure", phase: "A", step_id: "codex_reviewer", code: "TESTS_FAILED", head: "a".repeat(40), inputs_digest: digest },
      { kind: "failure", phase: "A", step_id: "codex_specifier", code: "WORKER_FAILED", head: "a".repeat(40), inputs_digest: digest },
      { kind: "failure", phase: "A", step_id: "codex_specifier", code: "WORKER_FAILED", head: "a".repeat(40), inputs_digest: digest },
    ]);
    // The structured code also names the automatic worker status.
    const bad = eventsOfKind(h, "worker").find((e: any) => e.status === "failed" && e.step_id === "codex_reviewer") as any;
    assert.equal(bad.blocker_code, "TESTS_FAILED");
    // The failure is recorded before the worker's terminal status, so a prerequisite can block the step directly.
    const order = eventsOf(h).map((e: any) => e.kind + ":" + (e.status ?? ""));
    assert.ok(order.indexOf("failure:") < order.indexOf("worker:failed"));
  });
});

test("a worker result that declares blocker_kind prerequisite is recorded as one even without an error", async () => {
  await withFailureBudget({}, {}, async (h) => {
    await h.handlers.get("tool_result")!(routeFor("A"));
    await codexStep(h, "p1", "A:codex_reviewer", { details: { blocker_code: "APPROVED_PILOT_INPUTS_UNAVAILABLE", blocker_kind: "prerequisite" } });
    await codexStep(h, "p2", "A:codex_specifier", { details: { blocker_code: "SOME_NOTE" } });
    assert.deepEqual(eventsOfKind(h, "failure").map((e: any) => [e.step_id, e.code, e.blocker_kind]),
      [["codex_reviewer", "APPROVED_PILOT_INPUTS_UNAVAILABLE", "prerequisite"]]);
  });
});

test("a refused start blocks only that call: no worker starts, the run and other tasks go on", async () => {
  const refuse = (input: Record<string, unknown>) => input.phase === "A" && input.step_id === "codex_reviewer"
    ? { code: "REPEATED_INEFFECTIVE_ACTION", failure_code: "TESTS_FAILED", count: 3, retry_budget: 2, signature: "f".repeat(64) } : null;
  await withFailureBudget({}, { gateRefusal: refuse }, async (h) => {
    await h.handlers.get("tool_result")!(routeFor("A"));
    await h.handlers.get("tool_result")!(routeFor("B"));
    const before = h.calls.length;
    const refused = await codexStep(h, "r1", "A:codex_reviewer");
    assert.equal(refused.guard.block, true);
    assert.match(refused.guard.reason, /^REPEATED_INEFFECTIVE_ACTION: step codex_reviewer of task A failed 3 times with TESTS_FAILED/);
    assert.match(refused.guard.reason, /other tasks may continue/);
    const after = h.calls.slice(before).filter((call) => call.script.endsWith("parent_orchestration.py")).map((call) => call.input?.kind);
    assert.deepEqual(after, ["retry_gate"], "nothing started: no activity, worker or snapshot event for the refused call");
    // The run is not blocked: status works, another task's worker starts, and so does another step of the same task.
    const status = await h.tools.get("orchestration_record").execute("s", { action: "status" });
    assert.equal(status.details.ok, true);
    assert.equal((await codexStep(h, "r2", "B:codex_reviewer")).guard, undefined);
    assert.equal((await codexStep(h, "r3", "A:codex_specifier")).guard, undefined);
    assert.equal(await h.handlers.get("tool_call")!({ toolName: "bash", toolCallId: "b", input: { command: "ls" } }), undefined);
  });
});

test("the gate asks with the checkout's HEAD, and a gate that cannot be evaluated lets the call through", async () => {
  await withFailureBudget({}, {}, async (h) => {
    await h.handlers.get("tool_result")!(routeFor("A", { workerMutation: "allowed" }));
    h.checkout.head = "c".repeat(40);
    await codexStep(h, "r1", "A:codex_reviewer");
    assert.equal((eventsOfKind(h, "retry_gate")[0] as any).head, "c".repeat(40));
    const heads = h.calls.filter((call) => call.script.endsWith("session_scope.py") && call.input?.operation === "head");
    assert.ok(heads.length >= 1);
  });
  // An unresolvable HEAD is sent as absent (the recorder treats it as "no state change").
  await withFailureBudget({}, {}, async (h) => {
    await h.handlers.get("tool_result")!(routeFor("A"));
    h.checkout.head = "";
    await codexStep(h, "r1", "A:codex_reviewer");
    assert.equal((eventsOfKind(h, "retry_gate")[0] as any).head, undefined);
  });
  await withFailureBudget({}, { failKind: "retry_gate" }, async (h) => {
    await h.handlers.get("tool_result")!(routeFor("A"));
    assert.equal((await codexStep(h, "r1", "A:codex_reviewer")).guard, undefined);
    assert.equal((await h.tools.get("orchestration_record").execute("s", { action: "status" })).details.ok, true);
  });
});

test("a lead cannot send failure or gate events", async () => {
  await withFailureBudget({}, {}, async (h) => {
    const params = h.tools.get("orchestration_record").parameters;
    for (const event of [{ kind: "failure", phase: "A", step_id: "s", code: "X" },
      { kind: "retry_gate", phase: "A", step_id: "s", retry_budget: 99, source: "cli" }]) {
      assert.equal(Value.Check(params, { action: "event", event }), false);
    }
  });
});

test("recorded end to end: the real recorder refuses the repeat, a new HEAD is a new signature, a prerequisite never trips", async () => {
  await withFailureBudget({ PI_ORCHESTRATION_FAILURE_RETRY_BUDGET: "1", PI_ORCHESTRATION_FAILURE_RETRY_BUDGET_SOURCE: "cli" }, {}, async (h) => {
    await h.handlers.get("tool_result")!(routeFor("A"));
    for (let attempt = 0; attempt < 2; attempt += 1) {
      await codexStep(h, `f${attempt}`, "A:codex_reviewer", { fail: true, details: { blocker_code: "TESTS_FAILED" } });
    }
    h.checkout.head = "9".repeat(40);
    await codexStep(h, "moved", "A:codex_reviewer", { fail: true, details: { blocker_code: "TESTS_FAILED" } });
    for (let attempt = 0; attempt < 4; attempt += 1) {
      await codexStep(h, `p${attempt}`, "A:codex_specifier", { fail: true, details: { blocker_code: "APPROVED_PILOT_INPUTS_UNAVAILABLE" } });
    }
    const replayed = replay([...eventsOf(h)], null);
    assert.deepEqual(replayed.errors, []);
    assert.deepEqual(replayed.parent.failure_signatures.map((s: any) => [s.step_id, s.code, s.kind, s.count]).sort(),
      [["codex_reviewer", "TESTS_FAILED", "failure", 1], ["codex_reviewer", "TESTS_FAILED", "failure", 2],
       ["codex_specifier", "APPROVED_PILOT_INPUTS_UNAVAILABLE", "prerequisite", 4]].sort());
    const workers = Object.fromEntries(replayed.parent.phases[0].workers.map((w: any) => [w.step_id, w]));
    assert.deepEqual([workers.codex_specifier.status, workers.codex_specifier.blocker_code], ["blocked", "APPROVED_PILOT_INPUTS_UNAVAILABLE"]);
    assert.equal(workers.codex_reviewer.status, "failed");
    // The stuck step gated again at the old HEAD, through the recorder: refused with the budget of 1.
    const gated = replay([...eventsOf(h), { kind: "retry_gate", phase: "A", step_id: "codex_reviewer", retry_budget: 1,
      source: "cli", head: "a".repeat(40) }], null);
    const blocked = gated.parent.phases[0].workers.find((w: any) => w.step_id === "codex_reviewer");
    assert.deepEqual([blocked.status, blocked.blocker_code], ["blocked", "REPEATED_INEFFECTIVE_ACTION"]);
    assert.equal(gated.parent.final_status, "IN_PROGRESS");
  });
});

test("an unbound step is measured at the HEAD of the task's bound mutating worktree, not the main checkout", async () => {
  await withFailureBudget({}, {}, async (h) => {
    await h.handlers.get("tool_result")!(routeFor("A", { workerMutation: "allowed" }));
    await h.handlers.get("tool_result")!(routeFor("B"));
    await h.tools.get("orchestration_record").execute("b", { action: "bind_workspace", task_id: "A",
      step_id: "claude_worker", path: "/tmp/wt-a" });
    h.checkout.head = "a".repeat(40);              // the main checkout never moves
    h.checkout.heads["/tmp/wt-a"] = "d".repeat(40); // the Claude worker committed in its own worktree
    // A read-only Codex reviewer of task A has no worktree of its own: it follows the task's.
    await codexStep(h, "r1", "A:codex_reviewer", { fail: true, details: { blocker_code: "TESTS_FAILED" } });
    // A task with no bound worktree falls back to the main checkout.
    await codexStep(h, "r2", "B:codex_reviewer", { fail: true, details: { blocker_code: "TESTS_FAILED" } });
    const heads = Object.fromEntries((eventsOf(h) as any[]).filter((e) => e.kind === "retry_gate" || e.kind === "failure")
      .map((e) => [`${e.kind}:${e.phase}`, e.head]));
    assert.deepEqual(heads, { "retry_gate:A": "d".repeat(40), "failure:A": "d".repeat(40),
      "retry_gate:B": "a".repeat(40), "failure:B": "a".repeat(40) });
  });
});

test("a prerequisite refusal reads differently from a loop refusal and also blocks only the call", async () => {
  const refuse = () => ({ code: "PREREQUISITE_UNCHANGED", failure_code: "MODEL_BACKEND_UNAVAILABLE", count: 1, retry_budget: 2, signature: "f".repeat(64) });
  await withFailureBudget({}, { gateRefusal: refuse }, async (h) => {
    await h.handlers.get("tool_result")!(routeFor("A"));
    const refused = await codexStep(h, "r1", "A:codex_reviewer");
    assert.match(refused.guard.reason, /^PREREQUISITE_UNCHANGED: step codex_reviewer of task A is blocked by the unresolved prerequisite MODEL_BACKEND_UNAVAILABLE/);
    assert.doesNotMatch(refused.guard.reason, /REPEATED_INEFFECTIVE_ACTION/);
    assert.equal((await h.tools.get("orchestration_record").execute("s", { action: "status" })).details.ok, true);
  });
});

// Worker sandbox (#63). Deterministic: the settings PICO hands Claude Code, the recorded posture, and the
// fail-closed check, with the platform probe mocked. No test starts a model or a real sandbox.
const SANDBOX_HOME = "/home/u";
const SANDBOX_CWD = "/repo/.pi/worktrees/s1";

test("a sandboxed worker may write only its worktree and declared caches, and reads no state or credentials", () => {
  const config = workerSandboxConfig({ allowed_domains: ["registry.npmjs.org", "*.pythonhosted.org"],
    cache_directories: ["pip"] }, {});
  const settings = workerSandboxSettings({ cwd: SANDBOX_CWD, mode: "full", config, home: SANDBOX_HOME,
    parentEnv: { PI_ORCHESTRATOR_STATE_DIR: "/state/pico" } }) as any;
  assert.deepEqual(Object.keys(settings).sort(), ["permissions", "sandbox"]);
  const { sandbox } = settings;
  // On, a hard gate, and no unsandboxed retry (dangerouslyDisableSandbox is ignored).
  assert.deepEqual([sandbox.enabled, sandbox.failIfUnavailable, sandbox.allowUnsandboxedCommands], [true, true, false]);
  // Writes: the working directory (the bound worktree) is Claude Code's default; only the declared cache is added.
  assert.deepEqual(sandbox.filesystem.allowWrite, ["/home/u/.cache/pip"]);
  for (const path of sandbox.filesystem.allowWrite) assert.ok(!path.startsWith("/state/pico") && !path.startsWith(SANDBOX_CWD));
  assert.deepEqual(sandbox.filesystem.denyWrite, ["/home/u/.cache/pi-coding-orchestrator", "/home/u/.config/pi-coding-orchestrator"]);
  // Reads: the PICO state directory (Garden/lease/provenance), the default state root, the gh config and the rest.
  for (const path of ["/state/pico", "/home/u/.local/state/pi-coding-orchestrator", "/home/u/.config/gh", "/home/u/.ssh",
    "/home/u/.git-credentials", "/home/u/.aws", "/home/u/.claude/.credentials.json"]) {
    assert.ok(sandbox.filesystem.denyRead.includes(path), path);
  }
  assert.equal(sandbox.filesystem.denyRead.length, 2 + SANDBOX_CREDENTIAL_PATHS.length,
    "the state dir and the XDG state root (no provenance fallback when the state dir is set), then credentials");
  for (const path of sandbox.filesystem.denyRead) assert.ok(!SANDBOX_CWD.startsWith(path), path);
  // Network: only the allowlist, denied deterministically instead of a prompt a headless worker cannot answer.
  assert.deepEqual(sandbox.network, { allowedDomains: ["registry.npmjs.org", "*.pythonhosted.org"], strictAllowlist: true });
  // The OS sandbox covers Bash only, so Read/Edit/Write get the same reads as deny rules, with `//` absolute paths.
  for (const rule of ["Read(//state/pico)", "Read(//state/pico/**)", "Read(//home/u/.config/gh/**)", "Read(//home/u/.ssh/**)"]) {
    assert.ok(settings.permissions.deny.includes(rule), rule);
  }
  assert.equal(settings.permissions.deny.length, 2 * sandbox.filesystem.denyRead.length);
  // XDG overrides move the protected and writable locations with them; GH_CONFIG_DIR is denied too.
  const moved = workerSandboxSettings({ cwd: SANDBOX_CWD, mode: "full", config, home: SANDBOX_HOME,
    parentEnv: { XDG_CACHE_HOME: "/xc", XDG_CONFIG_HOME: "/xg", XDG_STATE_HOME: "/xs", GH_CONFIG_DIR: "/ghc",
      PI_CODING_ORCHESTRATOR_HOME: "/pico-cache", PI_ORCHESTRATOR_STATE_DIR: "/state/pico" } }) as any;
  assert.deepEqual(moved.sandbox.filesystem.allowWrite, ["/xc/pip"]);
  assert.deepEqual(moved.sandbox.filesystem.denyWrite, ["/pico-cache", "/xg/pi-coding-orchestrator"]);
  for (const path of ["/xs/pi-coding-orchestrator", "/xg/gh", "/ghc"]) assert.ok(moved.sandbox.filesystem.denyRead.includes(path), path);
  // Without PI_ORCHESTRATOR_STATE_DIR, the state directory is this orchestrator checkout's provenance/.
  const local = workerSandboxSettings({ cwd: SANDBOX_CWD, mode: "full", config, home: SANDBOX_HOME, parentEnv: {} }) as any;
  assert.ok(local.sandbox.filesystem.denyRead.includes(join(import.meta.dirname, "..", "provenance")), "repo-local provenance");
});

test("worker network is off by default and the repository allowlist is validated", () => {
  const config = workerSandboxConfig(null, {});
  assert.deepEqual(config, { state: "enabled", source: "default", allowedDomains: [], cacheDirectories: [] });
  const settings = workerSandboxSettings({ cwd: SANDBOX_CWD, mode: "full", config, home: SANDBOX_HOME, parentEnv: {} }) as any;
  assert.deepEqual(settings.sandbox.network, { allowedDomains: [], strictAllowlist: true });
  assert.ok(!("allowWrite" in settings.sandbox.filesystem), "no extra writable directory");
  assert.deepEqual(workerSandboxConfig({ allowed_domains: ["a.example.org", "a.example.org"] }, {}).allowedDomains, ["a.example.org"]);
  for (const domain of ["*", "*.com", "10.0.0.1", "Example.org", "a.example.org/x", "a..org", "", 7]) {
    assert.throws(() => workerSandboxConfig({ allowed_domains: [domain] }, {}), WorkerConfigError, String(domain));
  }
  for (const cache of ["../x", "/abs", "~/.npm", "a/*", ".", "pi-coding-orchestrator", "pi-coding-orchestrator/runtimes"]) {
    assert.throws(() => workerSandboxConfig({ cache_directories: [cache] }, {}), WorkerConfigError, cache);
  }
  assert.throws(() => workerSandboxConfig({ allowed_domains: "pypi.org" }, {}), WorkerConfigError);
  assert.throws(() => workerSandboxConfig({ allowed_domains: Array.from({ length: 51 }, (_, i) => `h${i}.example.org`) }, {}),
    WorkerConfigError);
  // The posture comes from the environment, never from the repository.
  assert.equal(workerSandboxConfig({ allowed_domains: [] }, { PI_ORCHESTRATOR_WORKER_SANDBOX: "0" }).state, "opted_out");
});

test("read-only and opted-out workers get no sandbox block, only the read deny rules", () => {
  const enabled = workerSandboxConfig(null, {});
  const read = workerSandboxSettings({ cwd: SANDBOX_CWD, mode: "read", config: enabled, home: SANDBOX_HOME, parentEnv: {} }) as any;
  assert.deepEqual(Object.keys(read), ["permissions"], "a read-only worker has no Bash for the OS sandbox to wrap");
  const optedOut = workerSandboxConfig(null, { PI_ORCHESTRATOR_WORKER_SANDBOX: "0" });
  const full = workerSandboxSettings({ cwd: SANDBOX_CWD, mode: "full", config: optedOut, home: SANDBOX_HOME, parentEnv: {} }) as any;
  assert.deepEqual(Object.keys(full), ["permissions"]);
  assert.ok(full.permissions.deny.includes("Read(//home/u/.config/gh/**)"));
});

test("a boundary that cannot be expressed refuses the worker instead of widening it", () => {
  const config = workerSandboxConfig({ cache_directories: ["state/sub"] }, {});
  // The worktree inside a denied path: denying it would deny the worker its own directory.
  assert.throws(() => workerSandboxSettings({ cwd: "/state/pico/wt", mode: "full", config: workerSandboxConfig(null, {}),
    home: SANDBOX_HOME, parentEnv: { PI_ORCHESTRATOR_STATE_DIR: "/state/pico" } }), /is inside \/state\/pico/);
  assert.throws(() => workerSandboxSettings({ cwd: "/home/u/.cache/pi-coding-orchestrator/wt", mode: "full",
    config: workerSandboxConfig(null, {}), home: SANDBOX_HOME, parentEnv: {} }), WorkerConfigError);
  // A declared cache that overlaps the state directory.
  assert.throws(() => workerSandboxSettings({ cwd: SANDBOX_CWD, mode: "full", config, home: SANDBOX_HOME,
    parentEnv: { PI_ORCHESTRATOR_STATE_DIR: "/home/u/.cache/state" } }), /overlaps \/home\/u\/\.cache\/state/);
});

test("sandbox prerequisites are detected per platform", () => {
  const present = (paths: string[]) => (path: string) => paths.includes(path);
  const env = { PATH: "/usr/bin:relative:/opt/bin" };
  assert.deepEqual(detectWorkerSandbox(env, "linux", present(["/usr/bin/bwrap", "/opt/bin/socat"])),
    { available: true, detail: "bubblewrap and socat" });
  const noSocat = detectWorkerSandbox(env, "linux", present(["/usr/bin/bwrap", "relative/socat"]));
  assert.equal(noSocat.available, false);
  assert.match(noSocat.detail, /^socat not found on the worker PATH/);
  assert.match(detectWorkerSandbox({}, "linux", present(["/usr/bin/bwrap"])).detail, /^bwrap and socat not found/);
  assert.equal(detectWorkerSandbox({}, "darwin", present(["/usr/bin/sandbox-exec"])).available, true);
  assert.equal(detectWorkerSandbox({}, "darwin", present([])).available, false);
  assert.deepEqual(detectWorkerSandbox(env, "win32", present(["/usr/bin/bwrap", "/opt/bin/socat"])),
    { available: false, detail: "no Claude Code sandbox on platform win32" });
});

async function sandboxRun(options: { probe?: SandboxProbe; limits?: Record<string, unknown>; env?: Record<string, string>;
                                     mode?: "allowed" | "forbidden"; prompt?: string } = {}) {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "pico-claude-sandbox-")));
  const workspace = join(dir, "wt");
  mkdirSync(workspace);
  const previous = process.env.PI_ORCHESTRATOR_WORKER_SANDBOX;
  if (options.env?.PI_ORCHESTRATOR_WORKER_SANDBOX !== undefined) process.env.PI_ORCHESTRATOR_WORKER_SANDBOX = options.env.PI_ORCHESTRATOR_WORKER_SANDBOX;
  else delete process.env.PI_ORCHESTRATOR_WORKER_SANDBOX;
  try {
    const h = harness({ ok: true, status: "READY" }, false, true, { executable: fakeClaude(dir), sandboxProbe: options.probe,
      limits: options.limits });
    await h.handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, { cwd: dir, ui: { notify() {} } });
    await h.handlers.get("tool_result")!(routeFor("A", { workerMutation: options.mode ?? "allowed" }));
    if ((options.mode ?? "allowed") === "allowed") {
      await h.tools.get("orchestration_record").execute("b", { action: "bind_workspace", task_id: "A", step_id: "claude_worker", path: workspace });
    }
    const run = await claudeWorkerRun(h, "s", { prompt: options.prompt ?? "[pico-task A:claude_worker] go" });
    const recorded = h.parentCalls("event").map((call) => call.input!).filter((input) => input.kind === "worker_sandbox");
    return { h, run, recorded, dir };
  } finally {
    if (previous === undefined) delete process.env.PI_ORCHESTRATOR_WORKER_SANDBOX;
    else process.env.PI_ORCHESTRATOR_WORKER_SANDBOX = previous;
    rmSync(dir, { recursive: true, force: true });
  }
}

test("the sandbox is on by default, recorded at run start, and applies the repository allowlist", async () => {
  const plain = await sandboxRun();
  assert.deepEqual(plain.recorded, [{ kind: "worker_sandbox", state: "enabled", source: "default", allowed_domains: [],
    cache_directories: [], policy_source: "default" }]);
  const echoed = JSON.parse(plain.run.result.content[0].text);
  const settings = JSON.parse(echoed.args[echoed.args.indexOf("--settings") + 1]);
  assert.deepEqual(settings.sandbox.network, { allowedDomains: [], strictAllowlist: true });
  assert.deepEqual(plain.run.result.details.sandbox, { state: "enabled", source: "default", applied: true,
    allowed_domains: [], cache_directories: [] });

  const allowlisted = await sandboxRun({ limits: { ok: true, source: ".pi/autonomy.yaml", parallel_tasks: 3,
    worker_sandbox: { allowed_domains: ["registry.npmjs.org"], cache_directories: ["pip"] } } });
  assert.deepEqual(allowlisted.recorded, [{ kind: "worker_sandbox", state: "enabled", source: "default",
    allowed_domains: ["registry.npmjs.org"], cache_directories: ["pip"], policy_source: "policy" }]);
  const args = JSON.parse(allowlisted.run.result.content[0].text).args;
  const applied = JSON.parse(args[args.indexOf("--settings") + 1]).sandbox;
  assert.deepEqual(applied.network.allowedDomains, ["registry.npmjs.org"]);
  assert.equal(applied.filesystem.allowWrite.length, 1);
  assert.match(applied.filesystem.allowWrite[0], /\/pip$/);

  // An unreadable policy widens nothing: no network, recorded as invalid.
  const invalid = await sandboxRun({ limits: { ok: false, blocker_code: "AUTONOMY_POLICY_INVALID" } });
  assert.deepEqual([invalid.recorded[0].allowed_domains, invalid.recorded[0].policy_source], [[], "invalid"]);
});

test("an explicit opt-out runs the worker unsandboxed and stays on the run and the result", async () => {
  const { run, recorded } = await sandboxRun({ env: { PI_ORCHESTRATOR_WORKER_SANDBOX: "0" } });
  assert.deepEqual(recorded, [{ kind: "worker_sandbox", state: "opted_out", source: "env", allowed_domains: [],
    cache_directories: [], policy_source: "default" }]);
  assert.equal(run.result.isError, false, run.result.content[0].text);
  const args = JSON.parse(run.result.content[0].text).args;
  assert.ok(!("sandbox" in JSON.parse(args[args.indexOf("--settings") + 1])));
  assert.deepEqual(run.result.details.sandbox, { state: "opted_out", source: "env", applied: false,
    allowed_domains: [], cache_directories: [] });
  // An unrecognized value records no posture and refuses every worker.
  const bad = await sandboxRun({ env: { PI_ORCHESTRATOR_WORKER_SANDBOX: "off" } });
  assert.deepEqual(bad.recorded, []);
  assert.equal(bad.run.result.details.blocker_code, "WORKER_CONFIG_INVALID");
});

test("a mutating worker fails closed with WORKER_SANDBOX_UNAVAILABLE when the sandbox cannot start", async () => {
  const probe: SandboxProbe = () => ({ available: false, detail: "socat not found on the worker PATH (install bubblewrap and socat)" });
  const { run, h } = await sandboxRun({ probe });
  assert.equal(run.result.isError, true);
  assert.equal(run.result.details.error, "SANDBOX_UNAVAILABLE");
  assert.equal(run.result.details.blocker_code, "WORKER_SANDBOX_UNAVAILABLE");
  assert.match(run.result.content[0].text, /not started: the worker sandbox is unavailable: socat not found.*PI_ORCHESTRATOR_WORKER_SANDBOX=0/);
  assert.equal(run.result.details.exit_code, null, "Claude Code never started");
  const usage = h.calls.filter((call) => call.input?.kind === "usage").at(-1)!.input!;
  assert.deepEqual([usage.input_tokens, usage.output_tokens], [null, null]);
  const failed = h.parentCalls("event").map((call) => call.input!).filter((input) => input.kind === "worker").at(-1)!;
  assert.deepEqual([failed.status, failed.blocker_code], ["failed", "WORKER_SANDBOX_UNAVAILABLE"]);
  // With the explicit opt-out the same host runs the worker (and the record says so).
  const optedOut = await sandboxRun({ probe, env: { PI_ORCHESTRATOR_WORKER_SANDBOX: "0" } });
  assert.equal(optedOut.run.result.isError, false);
  // A read-only step has no Bash, so it never needs the sandbox.
  const read = await sandboxRun({ probe, mode: "forbidden", prompt: "[pico-task A:claude_worker] review" });
  assert.equal(read.run.result.isError, false, read.run.result.content[0].text);
  assert.equal(read.run.result.details.sandbox.applied, false);
});

test("Claude Code's own failIfUnavailable refusal is reported as WORKER_SANDBOX_UNAVAILABLE", async () => {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "pico-claude-refuse-")));
  try {
    const executable = join(dir, "claude");
    writeFileSync(executable, `#!/usr/bin/env node
process.stdin.resume(); process.stdin.on("end", () => {
  process.stderr.write("Error: sandbox unavailable detail: bubblewrap failed. Set sandbox.failIfUnavailable=false to allow unsandboxed execution.\\n");
  process.exit(1);
});
`, { mode: 0o755 });
    const outcome = await runClaudeWorker({ executable, cwd: dir, backendModel: "haiku", effort: "low", mode: "full",
      sandbox: workerSandboxConfig(null, {}) }, "go", undefined, 10_000, { probe: () => ({ available: true, detail: "test" }) });
    assert.equal(outcome.error, "SANDBOX_UNAVAILABLE");
    assert.equal(outcome.sandbox?.applied, true);
    // The same text without the sandbox applied stays an ordinary malformed result.
    const unsandboxed = await runClaudeWorker({ executable, cwd: dir, backendModel: "haiku", effort: "low", mode: "full",
      sandbox: workerSandboxConfig(null, { PI_ORCHESTRATOR_WORKER_SANDBOX: "0" }) }, "go", undefined, 10_000);
    assert.equal(unsandboxed.error, "MALFORMED_OUTPUT");
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
