/** Parent-run trace and backend readiness boundary for /orchestrate. */
import { spawn } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { realpathSync } from "node:fs";
import { hostname } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { classifyClaudeInvocationError, resolveClaudeBackendModel } from "./claude-backend-adapter.ts";
import { inGitDirectory, runClaudeWorker } from "./claude-worker.ts";
import type { ClaudeWorkerOutcome, ClaudeWorkerPlan } from "./claude-worker.ts";
import { initialBudgetState, observeContext } from "./context-budget.ts";
import type { BudgetState } from "./context-budget.ts";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const PARENT = join(ROOT, "tools", "parent_orchestration.py");
const PREFLIGHT = join(ROOT, "tools", "backend_preflight.py");
const SESSION_SCOPE = join(ROOT, "tools", "session_scope.py");
const CONTEXT = join(ROOT, "tools", "context_budget.py");
const AUTONOMY = join(ROOT, "tools", "autonomy_policy.py");
const LEASE = join(ROOT, "tools", "run_lease.py");
const DIFF_FLOORS = join(ROOT, "tools", "diff_floor_check.py");
const DIFF_UNDERRATED = "CLASSIFICATION_UNDERRATED_BY_DIFF";
const DIFF_UNVERIFIABLE = "CLASSIFICATION_DIFF_UNVERIFIABLE";
/** Renewal period of the single-owner run lease (tools/run_lease.py; default expiry 300 s). */
export const LEASE_HEARTBEAT_MS = 60_000;
/** Lease expiry assumed until the lease tool reports one. */
export const DEFAULT_LEASE_TTL_MS = 300_000;
/** Stop acting this long before the lease could expire without a successful renewal. */
export const LEASE_SAFETY_MARGIN_MS = 60_000;
/** Lease answers meaning another process now owns the run: this process detaches from it. */
const LEASE_LOST = new Set(["RUN_LEASE_LOST", "RUN_LEASE_NOT_HELD", "RUN_LEASE_PID_MISMATCH"]);
const LEASE_OWNER = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const LEASE_STATE = Symbol.for("pico.orchestration.lease");
const OWNER_ENV = "PI_ORCHESTRATION_LEASE_OWNER";
/** Parent refusals of a worker override that leave the record unchanged; they must never block the run. */
const HARMLESS_REFUSALS = new Set(["PARENT_WORKER_RECODE_REPEATED", "PARENT_WORKER_OVERRIDE_LIMIT"]);
/** Also harmless, but only for a worker the extension itself recorded: the lead restating it the old way. */
const RESTATEMENT_REFUSALS = new Set(["PARENT_EVENT_CONFLICT", "PARENT_WORKER_TRANSITION_INVALID"]);
const WORKER_TOOLS = new Set(["AskClaude", "ClaudeWorker", "spawn_subagent", "message_subagent"]);
const CLAUDE_TOOLS = new Set(["AskClaude", "ClaudeWorker"]);
function withoutAutomaticMarks(event: Json): Json {
  const { only_if_open: _a, auto_recorded: _b, ...rest } = event;
  return rest;
}
const MARKER = "PI_ORCHESTRATION_RUN_V1";
const SESSION_ENTRY = "pi-orchestration-parent-v1";
const MAX_OUTPUT = 256 * 1024;
const COMMAND_TIMEOUT_MS = 30_000;
const PHASE_STATUS = Type.Union([Type.Literal("not_started"), Type.Literal("in_progress"),
  Type.Literal("completed"), Type.Literal("blocked"), Type.Literal("failed"), Type.Literal("partial")]);
const WORKER_STATUS = Type.Union([Type.Literal("not_started"), Type.Literal("in_progress"),
  Type.Literal("completed"), Type.Literal("blocked"), Type.Literal("failed")]);
const ROUTE = Type.Union([Type.Literal("CODEX"), Type.Literal("CLAUDE"),
  Type.Literal("CLAUDE_CODEX"), Type.Literal("CODEX_CLAUDE_CODEX")]);
/**
 * The closed set of reasoning efforts a route result may assign to a step. It
 * is the only place the lifecycle names efforts: capacity tiers are never
 * named here (models are resolved generically through the route's
 * backend_bindings), so adding tiers or efforts only extends this constant.
 */
export const REASONING_EFFORTS: readonly string[] = ["low", "medium", "high"];
const EFFORT = Type.Union(REASONING_EFFORTS.map((effort) => Type.Literal(effort)));
/** Default `limits.parallel_tasks` when the target has no .pi/autonomy.yaml. */
const DEFAULT_PARALLEL_TASKS = 3;
/** Lead-call limit: set by the launcher (or the operator) in the environment, else by .pi/autonomy.yaml. */
const LEAD_LIMIT_ENV = "PI_ORCHESTRATION_MAX_LEAD_CALLS";
const LEAD_LIMIT_SOURCE_ENV = "PI_ORCHESTRATION_MAX_LEAD_CALLS_SOURCE";
const LEAD_WARN_ENV = "PI_ORCHESTRATION_LEAD_CALLS_WARN_AT";
const LEAD_LIMIT_SOURCES = new Set(["cli", "task", "manifest", "policy"]);
/**
 * Repeated ineffective actions: how many retries a step gets after the same structured failure (task, step,
 * route epoch, failure code, checkout HEAD, declared-input digest) before it is blocked with
 * REPEATED_INEFFECTIVE_ACTION. The launcher sets the environment; otherwise .pi/autonomy.yaml, else the default.
 */
const FAILURE_BUDGET_ENV = "PI_ORCHESTRATION_FAILURE_RETRY_BUDGET";
const FAILURE_BUDGET_SOURCE_ENV = "PI_ORCHESTRATION_FAILURE_RETRY_BUDGET_SOURCE";
const INPUTS_DIGEST_ENV = "PI_ORCHESTRATION_INPUTS_DIGEST";
const FAILURE_BUDGET_SOURCES = new Set(["cli", "task", "manifest", "policy"]);
export const DEFAULT_FAILURE_RETRY_BUDGET = 2;
const FAILURE_CODE = /^[A-Z][A-Z0-9_]{0,99}$/;
/** Lead calls allowed after the limit is reached, so the lead can record an explicit outcome. */
export const LEAD_CALL_GRACE = 2;
/** Longest wait for aborted workers to settle before the stop checkpoint and lease release. */
const LEAD_STOP_SETTLE_MS = 5000;
const TAG_ID = "[A-Za-z0-9][A-Za-z0-9_.-]{0,127}";
const LABEL_TAG = new RegExp(`^(${TAG_ID}):(${TAG_ID})$`);
const EXCERPT_CHARS = 300;
const SUMMARY_CHARS = 600;
const GOAL_CHARS = 200;
/**
 * Single-line, control-free text, cut generously (8x the recorded size). The recorder masks secret-like text in
 * this wider window first and only then cuts to the recorded size, so a token straddling that cut is masked whole.
 */
function rawWindow(value: string, recorded: number): string {
  return value.replace(/\u001b\[[0-9;?]*[ -/]*[@-~]/g, "").replace(/[\u0000-\u001f\u007f-\u009f]+/g, " ").replace(/\s+/g, " ").trim().slice(0, recorded * 8);
}
/** The goal line of a worker contract ("Goal: ..."), and the rest of it after the task tag and that line. */
function contractParts(contract: string): { goal?: string; excerpt: string } {
  let body = contract.replace(PROMPT_TAG, "").trim();
  const goal = /^[ \t]*Goal:[ \t]*(.+)$/im.exec(body);
  if (goal) body = `${body.slice(0, goal.index)}\n${body.slice(goal.index + goal[0].length)}`.trim();
  return { goal: goal?.[1], excerpt: body };
}
/** The commit a worker result reports ("commit abc1234", "at 244fea7", or a bare 40-hex id), if any. */
function reportedCommit(text: string): string | undefined {
  const keyed = /\b(?:commit(?:ted)?|sha|HEAD)\b[^0-9a-f]{0,12}([0-9a-f]{7,40})\b/i.exec(text)
    ?? /\b(?:at|as)\s+([0-9a-f]{7,40})\b(?=[\s;,.)]|$)/i.exec(text) ?? /\b([0-9a-f]{40})\b/.exec(text);
  return keyed ? keyed[1].toLowerCase() : undefined;
}
const PROMPT_TAG = new RegExp(`^\\s*\\[pico-task (${TAG_ID}):(${TAG_ID})\\]`);
const ESCALATION = Type.Union(["FAILED_ATTEMPTS", "REVIEW_FINDINGS", "AMBIGUITY", "ARCHITECTURE",
  "SECURITY_SENSITIVE", "SEMANTIC_RISK", "CONTEXT_BUDGET"].map((reason) => Type.Literal(reason)));
const SCORE = Type.Integer({ minimum: 0, maximum: 10 });
const ROUTING_SUMMARY = Type.Object({
  task_type: Type.String(), ambiguity: SCORE,
  ambiguity_dimensions: Type.Optional(Type.Record(Type.String(), SCORE)),
  work_profile: Type.Optional(Type.Record(Type.String(), SCORE)),
  failure_cost: Type.String(), semantic_correctness: Type.String(),
  diagnosis_required: Type.Boolean(), implementation_scope: Type.String(),
  signals: Type.Array(Type.String()), matches: Type.Array(ROUTE), fallback_applied: Type.Boolean(),
  assignments: Type.Array(Type.Object({ step_id: Type.String(), role: Type.String(),
    family: Type.String(), capacity_tier: Type.String(), ambiguity_capacity: SCORE,
    rule: Type.Union([Type.String(), Type.Null()]) }, { additionalProperties: false })),
}, { additionalProperties: false });
const EVENT = Type.Union([
  Type.Object({ kind: Type.Literal("escalation"), phase: Type.String(),
    from_route: ROUTE, to_route: ROUTE, reason: ESCALATION }, { additionalProperties: false }),
  Type.Object({ kind: Type.Literal("phase"), phase: Type.String(), status: PHASE_STATUS,
    route: Type.Optional(ROUTE), policy_version: Type.Optional(Type.Integer({ minimum: 1 })),
    routing: Type.Optional(ROUTING_SUMMARY), blocker_code: Type.Optional(Type.String()),
    escalation_reason: Type.Optional(ESCALATION) },
    { additionalProperties: false }),
  Type.Object({ kind: Type.Literal("worker"), phase: Type.String(), step_id: Type.String(),
    status: WORKER_STATUS, model: Type.Optional(Type.String()), backend: Type.Optional(Type.String()),
    backend_model: Type.Optional(Type.String()), reasoning_effort: Type.Optional(EFFORT),
    blocker_code: Type.Optional(Type.String()),
    installed_backend_version: Type.Optional(Type.String()),
    required_backend_version: Type.Optional(Type.String()) }, { additionalProperties: false }),
  Type.Object({ kind: Type.Literal("child"), phase: Type.String(), run_id: Type.String() },
    { additionalProperties: false }),
  Type.Object({ kind: Type.Literal("mutation"), github: Type.Optional(Type.Integer({ minimum: 0 })),
    code: Type.Optional(Type.Integer({ minimum: 0 })) }, { additionalProperties: false }),
  Type.Object({ kind: Type.Literal("interaction"), type: Type.Union([Type.Literal("directive"),
    Type.Literal("feedback"), Type.Literal("decision")]), summary: Type.String({ minLength: 1, maxLength: 180 }),
    task_id: Type.String({ pattern: `^${TAG_ID}$` }), step_id: Type.String({ pattern: `^${TAG_ID}$` }),
    subagent_id: Type.Optional(Type.String({ pattern: `^${TAG_ID}$` })),
    reply_to: Type.Optional(Type.Integer({ minimum: 1 })) }, { additionalProperties: false }),
]);

type Json = Record<string, unknown>;
type Guard = { block: true; reason: string; terminate?: boolean } | undefined;
function python(): string { return process.env.PI_ROUTER_PYTHON ?? "python3"; }
function samePath(left: string, right: string): boolean {
  try { return realpathSync(left) === realpathSync(right); } catch { return false; }
}
function isWorktreeAdd(call: { toolName: string; input: Record<string, unknown> }): boolean {
  return (call.toolName === "bash" || call.toolName === "powershell") && typeof call.input.command === "string"
    && /\bgit\s+(?:-\S+\s+)*worktree\s+add\b/.test(call.input.command);
}
/** Lease identity of this Pi process; kept on globalThis so an extension reload keeps it. */
export type LeaseProcessState = { nonce: string; launcherOwner: string | null };
export function leaseProcessState(env: NodeJS.ProcessEnv = process.env): LeaseProcessState {
  const store = globalThis as unknown as Record<symbol, LeaseProcessState | undefined>;
  let state = store[LEASE_STATE];
  if (!state) {
    state = { nonce: randomBytes(6).toString("hex"), launcherOwner: null };
    store[LEASE_STATE] = state;
  }
  const handed = env[OWNER_ENV];
  if (handed !== undefined) {
    // Read once and removed, so tools, workers and nested Pi processes never inherit the owner.
    delete env[OWNER_ENV];
    if (LEASE_OWNER.test(handed)) state.launcherOwner = handed;
  }
  return state;
}

/**
 * Lease owner id of this Pi process for a session. A launcher (headless_run)
 * hands its own owner id down through PI_ORCHESTRATION_LEASE_OWNER; otherwise
 * host, pid, a per-process random nonce and the session make it stable across
 * a reload in the same process and distinct for any other process.
 */
export function leaseOwner(sessionId: string | null, launcherOwner: string | null, pid: number, host: string,
                           nonce: string): string {
  if (launcherOwner && LEASE_OWNER.test(launcherOwner)) return launcherOwner;
  const clean = (value: string, size: number) => value.replace(/[^A-Za-z0-9_.-]/g, "-").slice(0, size);
  return `pi-${clean(host || "host", 32)}-${pid}-${clean(nonce, 16)}-${clean(sessionId ?? "nosession", 40)}`.slice(0, 128);
}

function count(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

function completionSummary(result: Json, runId: string): string {
  const metrics = (result.orchestration_metrics ?? {}) as Json;
  const parent = (result.parent_orchestration ?? {}) as Json;
  const context = (parent.context ?? {}) as Json;
  const usage = (context.worker_usage ?? {}) as Record<string, Json>;
  const workers = Object.values(usage).reduce((sum, entry) => sum + (count(entry.invocations) ?? 0), 0);
  const models = ((parent.phases ?? []) as Json[]).flatMap((phase) =>
    ((phase.workers ?? []) as Json[]).map((worker) => worker.model).filter((model): model is string => typeof model === "string"));
  const value = (item: unknown) => item === null || item === undefined ? "unknown" : String(item);
  const tracePath = typeof result.trace_path === "string" ? result.trace_path : `provenance/runs/${runId}.json`;
  return `Parent orchestration run ${runId} finalized.\n` +
    `Run stats: ${value(metrics.wall_clock_ms)} ms wall time; ${value(metrics.spawned_agents)} subagents spawned; ` +
    `${workers} worker calls; lead model ${value(metrics.lead_model)}; worker models ${models.length ? [...new Set(models)].join(", ") : "none observed"}; ` +
    `lead tokens ${value(metrics.lead_input_tokens)}/${value(metrics.lead_output_tokens)} input/output; ` +
    `worker tokens ${value(metrics.worker_input_tokens)}/${value(metrics.worker_output_tokens)} input/output; ` +
    `total tokens ${value(metrics.total_tokens)}.\n` +
    `Trace: ${tracePath}\n` +
    `Render it with: python3 ${join(ROOT, "tools", "backlog.py")} trace ${runId}`;
}

function assignment(route: Json, stepId: unknown): Json | undefined {
  const assignments = Array.isArray(route.capacity_assignments) ? route.capacity_assignments as Json[] : [];
  return assignments.find((item) => item.step_id === stepId);
}

/** Keep only policy metadata needed to explain a routed worker in the viewer. */
function routingSummary(route: Json): Json | null {
  const effective = ((route.classification as Json)?.effective ?? {}) as Json;
  const dimensionNames = ["outcome", "contract", "design", "evidence", "authority"];
  const modeNames = ["search", "analysis", "writing", "implementation", "verification", "coordination"];
  const scores = (value: unknown, names: string[]): Record<string, number> | undefined => {
    if (!value || typeof value !== "object") return undefined;
    const source = value as Json;
    if (!names.every((name) => count((source[name] as Json)?.level ?? source[name]) !== null)) return undefined;
    return Object.fromEntries(names.map((name) => [name, count((source[name] as Json)?.level ?? source[name])!])) as Record<string, number>;
  };
  const plan = ((route.execution_plan as Json)?.steps ?? []) as Json[];
  const assignments = plan.map((step) => {
    const selected = assignment(route, step.id) ?? {};
    const profile = (selected.selected_model_profile ?? {}) as Json;
    return { step_id: step.id, role: step.role, family: step.family,
      capacity_tier: selected.assigned_capacity_tier,
      ambiguity_capacity: profile.ambiguity_capacity, rule: selected.rule ?? null };
  });
  if (typeof effective.task_type !== "string" || count(effective.ambiguity) === null ||
      typeof effective.failure_cost !== "string" || typeof effective.semantic_correctness !== "string" ||
      typeof effective.diagnosis_required !== "boolean" || typeof effective.implementation_scope !== "string" ||
      !Array.isArray((route.matches as Json)?.effective) || typeof route.fallback_applied !== "boolean" ||
      assignments.some((item) => typeof item.step_id !== "string" || typeof item.role !== "string" ||
        typeof item.family !== "string" || typeof item.capacity_tier !== "string" ||
        count(item.ambiguity_capacity) === null)) return null;
  return { task_type: effective.task_type, ambiguity: effective.ambiguity,
    ...(scores(effective.ambiguity_assessment, dimensionNames) ?
      { ambiguity_dimensions: scores(effective.ambiguity_assessment, dimensionNames) } : {}),
    ...(scores(effective.work_profile, modeNames) ? { work_profile: scores(effective.work_profile, modeNames) } : {}),
    failure_cost: effective.failure_cost, semantic_correctness: effective.semantic_correctness,
    diagnosis_required: effective.diagnosis_required, implementation_scope: effective.implementation_scope,
    signals: ["assumptions_must_be_explicit", "security_sensitive", "destructive_operation", "cross_layer",
      "production_feature", "many_routine_decisions"].filter((key) => effective[key] === true).concat(
      effective.behavior_preservation === "required" ? ["behavior_preservation_required"] : []),
    matches: (route.matches as Json)?.effective, fallback_applied: route.fallback_applied,
    ...(Array.isArray(route.path_floor_matches) && route.path_floor_matches.length > 0
      ? { path_floor_matches: route.path_floor_matches } : {}),
    assignments };
}

/** Effort the routed plan assigned to a step; null when the result does not carry one. */
function assignedEffort(route: Json, stepId: unknown): string | null {
  const effort = assignment(route, stepId)?.assigned_reasoning_effort;
  return typeof effort === "string" && REASONING_EFFORTS.includes(effort) ? effort : null;
}

/** Canonical model bound to a step's assigned capacity tier, whatever tiers the policy defines. */
function assignedModel(route: Json, step: Json): string | null {
  const tier = assignment(route, step.id)?.assigned_capacity_tier;
  const bindings = (route.backend_bindings ?? {}) as Record<string, Json>;
  const family = typeof step.family === "string" ? bindings[step.family] : undefined;
  const models = (family?.capacity_models ?? {}) as Record<string, Json>;
  const model = typeof tier === "string" ? models[tier]?.model : undefined;
  return typeof model === "string" ? model : null;
}

/** One routed step the parent may launch a worker for. */
export type StepApproval = { id: string; family: string; model: string | null; effort: string | null;
  mode: "read" | "full" };
/** Per-task approval created by a successful route_task + preflight; replaced only by re-routing that task. */
export type RouteApproval = { taskId: string; route: string; phase: string; worktreeReady: boolean;
  taskType: string | null; claudeBackend: Json | null; steps: Map<string, StepApproval>;
  /** The complete route_task result, re-validated by the post-diff path-floor check at integrate_begin. */
  result: Json };
export type Attribution = { taskId: string; stepId: string };
/** A ClaudeWorker run resolved by the guard from the task's route approval. */
type WorkerPlan = ClaudeWorkerPlan & { taskId: string; stepId: string; phase: string; canonical: string };

/** Blocker code of a failed ClaudeWorker run; backend text is classified only on explicit evidence. */
function workerBlocker(outcome: ClaudeWorkerOutcome): string {
  switch (outcome.error) {
    case "TIMEOUT": return "WORKER_TIMEOUT";
    case "ABORTED": return "WORKER_ABORTED";
    case "OUTPUT_LIMIT": return "WORKER_OUTPUT_LIMIT";
    case "SPAWN_FAILED": return "MODEL_BACKEND_UNAVAILABLE";
    case "CONFIG_INVALID": return "WORKER_CONFIG_INVALID";
    default: return classifyClaudeInvocationError(outcome.text);
  }
}

/**
 * Task/step a worker call claims. spawn_subagent uses `label: "<task_id>:<step_id>"`;
 * ClaudeWorker and AskClaude (which have no label) start their prompt with
 * `[pico-task <task_id>:<step_id>]`.
 */
export function workerAttribution(toolName: string, input: Record<string, unknown>): Attribution | null {
  const match = toolName === "spawn_subagent" && typeof input.label === "string" ? LABEL_TAG.exec(input.label.trim())
    : CLAUDE_TOOLS.has(toolName) && typeof input.prompt === "string" ? PROMPT_TAG.exec(input.prompt) : null;
  return match ? { taskId: match[1], stepId: match[2] } : null;
}

/** Literal directory prefix of a relative path or glob, as segments; null when the entry is unsafe. */
function scopePrefix(entry: string): string[] | null {
  const path = entry.trim().replace(/\\/g, "/").replace(/^\.\//, "");
  if (!path || path.startsWith("/") || /^[A-Za-z]:/.test(path)) return null;
  const segments = path.split("/").filter((segment) => segment !== "" && segment !== ".");
  if (segments.includes("..")) return null;
  const glob = segments.findIndex((segment) => /[*?[\]{}]/.test(segment));
  return glob === -1 ? segments : segments.slice(0, glob);
}

/**
 * Conservative overlap of two write-scope entries: they overlap when one's
 * literal prefix contains the other's. `src/a/**` and `src/b/**` are disjoint;
 * `src/*.ts` overlaps everything under `src/`; `**` overlaps everything.
 */
export function scopesOverlap(left: string, right: string): boolean {
  const a = scopePrefix(left);
  const b = scopePrefix(right);
  if (!a || !b) return true;
  const shorter = a.length <= b.length ? a : b;
  const longer = shorter === a ? b : a;
  return shorter.every((segment, index) => longer[index] === segment);
}

export function runJson(script: string, args: string[], input?: Json, timeoutMs = COMMAND_TIMEOUT_MS): Promise<Json> {
  return new Promise((resolve) => {
    const child = spawn(python(), [script, ...args], { cwd: ROOT, stdio: ["pipe", "pipe", "pipe"] });
    let output = "";
    let settled = false;
    const finish = (result: Json) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      finish({ ok: false, component: "orchestration_subprocess",
        operation: args[0] ?? "preflight", error_category: "Timeout",
        orchestrator_root: ROOT, resource_path: script });
    }, timeoutMs);
    child.stdout.on("data", (part: Buffer) => { output = (output + part.toString("utf8")).slice(0, MAX_OUTPUT); });
    child.on("error", (error) => finish({ ok: false, component: "orchestration_subprocess",
      operation: args[0] ?? "preflight", error_category: error.name,
      orchestrator_root: ROOT, resource_path: script }));
    child.on("close", (code) => {
      try {
        const result = JSON.parse(output) as Json;
        finish(code === 0 || result.ok === false ? result : { ok: false,
          component: "orchestration_subprocess", operation: args[0] ?? "preflight",
          error_category: "NonzeroExit", orchestrator_root: ROOT, resource_path: script });
      } catch { finish({ ok: false, component: "orchestration_subprocess",
        operation: args[0] ?? "preflight", error_category: "MalformedOutput",
        orchestrator_root: ROOT, resource_path: script }); }
    });
    child.stdin.end(input === undefined ? "" : JSON.stringify(input));
  });
}

export function createOrchestrationLifecycle(command: typeof runJson = runJson, clock: () => number = Date.now) {
return function orchestrationLifecycle(pi: ExtensionAPI) {
  let runId: string | null = null;
  let lastRunId: string | null = null;
  let lastTerminal: Json | null = null;
  let parentBlocked = false;
  let parentFailure: Json | null = null;
  const pendingReroutes = new Set<string>();
  const claudeCalls = new Map<string, { canonical: string; backend: string }>();
  // task_id -> the route approval for that task. Routing task B never touches task A.
  const approvals = new Map<string, RouteApproval>();
  // route_task toolCallId -> task id it routes, so a failed result clears only that task.
  const pendingRoutes = new Map<string, string>();
  // "<task_id>\0<step_id>" -> linked worktree a mutating worker is bound to for this run.
  const workspaces = new Map<string, string>();
  // Process lifetime: subagent id -> directory it was spawned in.
  const spawnedSubagents = new Map<string, string>();
  // Parent run: subagent id -> task/step it was spawned for.
  const subagentTasks = new Map<string, Attribution>();
  // toolCallId -> parent checkout snapshot taken before a worker call.
  const snapshots = new Map<string, { digest: string; status: string[] }>();
  // toolCallId -> worker call currently running.
  const inFlight = new Map<string, { taskId: string | null; stepId: string | null; tool: string; phase?: string }>();
  // toolCallId -> ClaudeWorker run the guard resolved; consumed by the tool's execute.
  const workerPlans = new Map<string, WorkerPlan>();
  let scopeViolation: string | null = null;
  let lastPhase = "routing";
  let currentTurn: number | null = null;
  const messageStarts = new Map<number, number>();
  // The explicitly declared wave: the only one under which tasks may run concurrently.
  let activeWave: { id: string; tasks: Map<string, string[]> } | null = null;
  // Implicit single-task waves (one per task) recorded in this process and not yet integrated. They never
  // allow concurrency; "auto" needs exactly one.
  const implicitOpen = new Map<string, string[]>();
  // Route epoch (escalations seen) of each task, and the epoch each implicit wave was recorded in.
  const routeEpochs = new Map<string, number>();
  const implicitEpoch = new Map<string, number>();
  // Workers (phase, step) whose start or terminal status this extension recorded.
  const autoWorkers = new Set<string>();
  let integrating: string | null = null;
  let batchStart = 0;
  let batchPeak = 0;
  let targetCwd: string | null = null;
  // Session scope: only subagents spawned during this parent run may be continued.
  const runSubagents = new Set<string>();
  const workerStarts = new Map<string, number>();
  let budget: BudgetState | null = null;
  let recordedPeak = 0;
  let compactionFailed = false;
  // Lead-call limit of the active run; null when none is configured (then nothing below applies).
  let leadLimit: { max: number; source: string; calls: number; warnAt: number | null; warned: boolean; reached: boolean } | null = null;
  // Set once the extension itself stopped the run at the limit (left IN_PROGRESS for a resume): every later tool call is refused.
  let limitStopped: string | null = null;
  // Retry budget for repeated failure signatures; resolved at run start (null before that and after the run).
  let failureBudget: { value: number; source: string } | null = null;
  let sessionId: string | null = null;
  let leaseTimer: ReturnType<typeof setInterval> | null = null;
  const leaseIdentity = leaseProcessState();
  // Time of the last successful lease grant or renewal, and the lease's expiry.
  let leaseRenewedAt = 0;
  let leaseTtlMs = DEFAULT_LEASE_TTL_MS;
  let lastCtx: ExtensionContext | null = null;
  // toolCallId -> abort handle of a ClaudeWorker run, so a lost lease stops it.
  const workerAborts = new Map<string, AbortController>();

  const owner = () => leaseOwner(sessionId, leaseIdentity.launcherOwner, process.pid, hostname(), leaseIdentity.nonce);
  const ownerArgs = () => ["--owner", owner()];
  const leaseArgs = () => [...ownerArgs(), "--pid", String(process.pid)];
  function stopHeartbeat(): void {
    if (leaseTimer) clearInterval(leaseTimer);
    leaseTimer = null;
  }
  function startHeartbeat(result: Json): void {
    stopHeartbeat();
    leaseGranted(result);
    leaseTimer = setInterval(() => { void renewLease(); }, LEASE_HEARTBEAT_MS);
    leaseTimer.unref?.();
  }
  function leaseGranted(result: Json): void {
    leaseRenewedAt = clock();
    const ttl = count((result.lease as Json | undefined)?.ttl_seconds);
    if (ttl) leaseTtlMs = ttl * 1000;
  }
  /** True once the lease may have expired without this process knowing: it must stop acting. */
  function leaseLapsed(): boolean {
    return runId !== null && clock() >= leaseRenewedAt + leaseTtlMs - Math.min(LEASE_SAFETY_MARGIN_MS, leaseTtlMs / 2);
  }
  /**
   * Leave the run to whoever owns it now: block every tool, abort work already
   * running under the lost lease, and never settle, finish or write to it.
   */
  function detach(result: Json): void {
    const id = runId;
    stopHeartbeat();
    runId = null;
    if (id) lastRunId = id;
    parentBlocked = true;
    parentFailure = result;
    blockAllWorktrees();
    for (const controller of workerAborts.values()) controller.abort();
    workerAborts.clear();
    if (inFlight.size > 0) lastCtx?.abort?.();
    resetRunState();
    diagnose("lease", result);
  }
  function selfExpire(cause: unknown = null): void {
    detach({ ok: false, error_code: "RUN_LEASE_SELF_EXPIRED", error_category: "LeaseNotRenewed",
      run_id: runId, cause });
  }
  /** Renew the run lease; detach when another owner holds it or renewals failed for too long. */
  async function renewLease(): Promise<void> {
    const id = runId;
    if (!id) { stopHeartbeat(); return; }
    const result = await command(LEASE, ["heartbeat", id, ...leaseArgs()]);
    if (runId !== id) return;
    if (result.ok !== false) { leaseGranted(result); return; }
    diagnose("lease_heartbeat", result);
    if (LEASE_LOST.has(String(result.error_code))) detach(result);
    else if (leaseLapsed()) selfExpire(result.error_code ?? result.error_category ?? null);
  }

  const workspaceKey = (taskId: string, stepId: string) => `${taskId}\u0000${stepId}`;
  const worktreeReady = () => !parentBlocked && [...approvals.values()].some((approval) => approval.worktreeReady);
  const anyClaudeBackend = () => [...approvals.values()].find((approval) => approval.claudeBackend)?.claudeBackend ?? null;
  const blockAllWorktrees = () => { for (const approval of approvals.values()) approval.worktreeReady = false; };

  function persist(active: boolean): void {
    if (sessionId && lastRunId && targetCwd) pi.appendEntry(SESSION_ENTRY,
      { run_id: lastRunId, session_id: sessionId, target_cwd: targetCwd, active });
  }

  async function restore(ctx: ExtensionContext): Promise<void> {
    const manager = ctx.sessionManager;
    if (!manager) return;
    sessionId = manager.getSessionId();
    const entries = manager.getBranch();
    const marker = [...entries].reverse().find((entry) => entry.type === "custom" && entry.customType === SESSION_ENTRY);
    if (!marker || marker.type !== "custom") return;
    const data = marker.data as Json | undefined;
    if (!data || data.active !== true || data.session_id !== sessionId) return;
    if (typeof data.run_id !== "string" || typeof data.target_cwd !== "string") {
      parentBlocked = true;
      parentFailure = { ok: false, error_code: "PARENT_SESSION_MARKER_INVALID",
        error_category: "InvalidSessionEntry" };
      return;
    }
    if (!samePath(data.target_cwd, ctx.cwd)) {
      lastRunId = data.run_id;
      parentBlocked = true;
      parentFailure = { ok: false, error_code: "SESSION_REPOSITORY_MISMATCH",
        error_category: "TargetWorktreeChanged", run_id: data.run_id };
      return;
    }
    targetCwd = ctx.cwd;
    // Resume = verify identity, take the single-owner run lease, verify the
    // resume checkpoint. A held or stale lease and checkpoint drift fail closed.
    const args = ["resume", data.run_id, "--worktree", ctx.cwd];
    if (process.env.PI_TARGET_REPOSITORY) args.push("--repo", process.env.PI_TARGET_REPOSITORY);
    args.push(...leaseArgs());
    // A launcher-held lease moves to this process only through the explicit hand-off.
    if (leaseIdentity.launcherOwner) args.push("--handoff");
    const result = await command(PARENT, args);
    if (result.status !== "IN_PROGRESS" || result.run_id !== data.run_id) {
      if (result.ok === false) { parentBlocked = true; parentFailure = result; lastRunId = data.run_id; }
      else { lastRunId = data.run_id; lastTerminal = result; persist(false); }
      return;
    }
    runId = data.run_id;
    lastRunId = runId;
    targetCwd = ctx.cwd;
    parentBlocked = false;
    parentFailure = null;
    // Runtime permissions, waves, and worker handles are deliberately not restored.
    resetRunState();
    for (const phase of Array.isArray(result.reroute_phases) ? result.reroute_phases : []) {
      if (typeof phase === "string") pendingReroutes.add(phase);
    }
    budget = initialBudgetState();
    startHeartbeat(result);
    // A resumed run is bound by the effective limit too, counting from the calls already made.
    await configureLeadLimit();
    await configureFailureBudget();
  }

  function diagnose(operation: string, result: Json): void {
    const diagnostic = { component: result.component ?? "parent_orchestration",
      operation, run_id: result.run_id ?? runId, event_kind: result.event_kind ?? null,
      lifecycle_state: result.lifecycle_state ?? (parentBlocked ? "BLOCKED" : "IN_PROGRESS"),
      error_code: result.error_code ?? result.blocker_code ?? "PARENT_OPERATION_FAILED",
      error_category: result.error_category ?? "OperationRejected",
      error: result.error ?? null, validation_path: result.validation_path ?? null,
      orchestrator_commit: result.orchestrator_commit ?? null,
      orchestrator_root: ROOT, resource_path: result.resource_path ?? PARENT };
    process.stderr.write(`[orchestration-lifecycle] ${JSON.stringify(diagnostic)}\n`);
  }

  async function event(data: Json): Promise<boolean> {
    if (!runId) return false;
    const result = await command(PARENT, ["event", runId, ...ownerArgs()], data);
    if (result.ok === false) {
      if (LEASE_LOST.has(String(result.error_code))) { detach(result); return false; }
      diagnose("event", result); parentFailure = result;
      parentBlocked = true; blockAllWorktrees(); return false;
    }
    return true;
  }
  /** Telemetry is observational: a failed write is diagnosed but never blocks the run. */
  async function observe(data: Json): Promise<void> {
    if (!runId) return;
    const result = await command(PARENT, ["event", runId, ...ownerArgs()], data);
    if (result.ok === false) diagnose("telemetry", result);
  }
  function resetRunState(): void {
    approvals.clear(); pendingRoutes.clear(); workspaces.clear(); snapshots.clear();
    subagentTasks.clear(); inFlight.clear(); claudeCalls.clear(); workerPlans.clear();
    scopeViolation = null; activeWave = null; integrating = null; implicitOpen.clear(); routeEpochs.clear(); implicitEpoch.clear(); autoWorkers.clear(); lastPhase = "routing";
    currentTurn = null; messageStarts.clear(); leadLimit = null; failureBudget = null;
    batchStart = 0; batchPeak = 0;
    pendingReroutes.clear();
  }
  /**
   * Resolve this run's lead-call limit and warning threshold (explicit env from the launcher, else the repository
   * policy) and record them. An invalid value blocks the run closed: a limit that cannot be honored is never
   * silently dropped.
   */
  async function configureLeadLimit(): Promise<void> {
    leadLimit = null;
    if (!runId) return;
    const invalid = (code: string) => {
      parentBlocked = true; blockAllWorktrees();
      parentFailure = { ok: false, error_code: code, error_category: "InvalidLeadCallLimit" };
      diagnose("lead_limit", parentFailure);
    };
    let max: number | null = null;
    let source = "policy";
    let warnAt: number | null = null;
    const raw = process.env[LEAD_LIMIT_ENV];
    const rawWarn = process.env[LEAD_WARN_ENV];
    const positive = /^[1-9][0-9]{0,8}$/;
    if (raw !== undefined && raw !== "") {
      if (!positive.test(raw)) return invalid("MAX_LEAD_CALLS_INVALID");
      max = Number(raw);
      const named = process.env[LEAD_LIMIT_SOURCE_ENV];
      source = named !== undefined && LEAD_LIMIT_SOURCES.has(named) ? named : "env";
      if (rawWarn !== undefined && rawWarn !== "") {
        if (!positive.test(rawWarn)) return invalid("LEAD_CALLS_WARN_AT_INVALID");
        warnAt = Number(rawWarn);
      }
    }
    if (max === null || warnAt === null) {
      const limits = await command(AUTONOMY, ["limits", "--worktree", targetCwd ?? "."]);
      if (limits.ok !== false) {
        if (max === null) max = count(limits.max_lead_calls) || null;
        // The repository's threshold applies only below the effective limit; otherwise the 80% default does.
        const inherited = count(limits.lead_calls_warn_at);
        if (warnAt === null && max !== null && inherited !== null && inherited >= 1 && inherited < max) warnAt = inherited;
      }
    }
    if (max === null) return;
    if (warnAt !== null && warnAt >= max) return invalid("LEAD_CALLS_WARN_AT_INVALID");
    // Default: 80% of the limit, rounded down; no warning when that is below the first call.
    if (warnAt === null) { const derived = Math.floor(max * 0.8); warnAt = derived >= 1 && derived < max ? derived : null; }
    const result = await command(PARENT, ["event", runId, ...ownerArgs()],
      { kind: "lead_limit", state: "set", max_lead_calls: max, source, ...(warnAt === null ? {} : { warn_at: warnAt }) });
    if (result.ok === false) {
      if (LEASE_LOST.has(String(result.error_code))) detach(result);
      else { diagnose("lead_limit", result); parentFailure = result; parentBlocked = true; blockAllWorktrees(); }
      return;
    }
    // A resumed run keeps counting from the calls already recorded; the count never resets.
    // The count is the larger of the recorded telemetry and the extension's own earlier count.
    const recorded = result.context as Json | undefined;
    const telemetry = Array.isArray(recorded?.lead_calls) ? (recorded!.lead_calls as unknown[]).length : 0;
    const own = count((recorded?.lead_call_limit as Json | undefined)?.calls_seen) ?? 0;
    leadLimit = { max, source, calls: Math.max(telemetry, own), warnAt, warned: false, reached: false };
    await crossThresholds();
  }
  /**
   * Resolve the per-signature retry budget (launcher environment, else the repository policy, else the default).
   * An invalid value blocks the run closed rather than being dropped.
   */
  async function configureFailureBudget(): Promise<void> {
    failureBudget = null;
    if (!runId) return;
    const invalid = () => {
      parentBlocked = true; blockAllWorktrees();
      parentFailure = { ok: false, error_code: "FAILURE_RETRY_BUDGET_INVALID", error_category: "InvalidFailureRetryBudget" };
      diagnose("failure_retry_budget", parentFailure);
    };
    const raw = process.env[FAILURE_BUDGET_ENV];
    if (raw !== undefined && raw !== "") {
      if (!/^(0|[1-9][0-9]{0,3})$/.test(raw)) return invalid();
      const named = process.env[FAILURE_BUDGET_SOURCE_ENV];
      failureBudget = { value: Number(raw), source: named !== undefined && FAILURE_BUDGET_SOURCES.has(named) ? named : "env" };
      return;
    }
    const limits = await command(AUTONOMY, ["limits", "--worktree", targetCwd ?? "."]);
    const configured = limits.ok === false ? null : limits.failure_retry_budget;
    if (configured !== undefined && configured !== null) {
      if (typeof configured !== "number" || !Number.isInteger(configured) || configured < 0 || configured > 1000) return invalid();
      failureBudget = { value: configured, source: "policy" };
      return;
    }
    failureBudget = { value: DEFAULT_FAILURE_RETRY_BUDGET, source: "default" };
  }
  /**
   * The HEAD a step's state is measured at: its own bound worktree; for an unbound step (a read-only reviewer) the
   * task's bound mutating worktree, where the task's real commits land; the target checkout only when the task has
   * none. Null when it cannot be resolved: the recorder then treats the state as unchanged.
   */
  async function headOf(taskId: string, stepId: string): Promise<string | null> {
    const own = workspaces.get(workspaceKey(taskId, stepId));
    const prefix = `${taskId}\u0000`;
    const taskBound = [...workspaces].find(([key]) => key.startsWith(prefix))?.[1];
    const path = own ?? taskBound ?? targetCwd;
    if (!path) return null;
    const result = await command(SESSION_SCOPE, [], { operation: "head", path });
    if (result.ok === true && typeof result.head === "string" && /^[0-9a-f]{40,64}$/.test(result.head)) return result.head;
    diagnose("failure_head", { ok: false, error_code: "FAILURE_HEAD_UNKNOWN", error_category: "HeadUnresolved", run_id: runId });
    return null;
  }
  /** Digest of the declared inputs' status, set by the launcher; null when the run has none. */
  function inputsDigest(): string | null {
    const value = process.env[INPUTS_DIGEST_ENV];
    return value !== undefined && /^[0-9a-f]{64}$/.test(value) ? value : null;
  }
  /**
   * The retry gate: before a routed worker starts, ask the record whether this step's current failure signature
   * (same HEAD and inputs) has used up the retry budget. A refusal blocks only this call: the step is recorded
   * blocked, the run and the other tasks continue. A gate that cannot be evaluated is diagnosed and lets the
   * call through (it only guards against wasted work, never against an unsafe action).
   */
  async function retryGate(taskId: string, phase: string, stepId: string): Promise<string | undefined> {
    if (!runId || !failureBudget) return undefined;
    const head = await headOf(taskId, stepId);
    const digest = inputsDigest();
    const result = await command(PARENT, ["event", runId, ...ownerArgs()],
      { kind: "retry_gate", phase, step_id: stepId, retry_budget: failureBudget.value, source: failureBudget.source,
        ...(head ? { head } : {}), ...(digest ? { inputs_digest: digest } : {}) });
    if (result.ok === false) {
      if (LEASE_LOST.has(String(result.error_code))) detach(result);
      else diagnose("retry_gate", result);
      return undefined;
    }
    const gate = result.retry_gate as Json | undefined;
    if (gate?.refused !== true) return undefined;
    if (gate.code === "PREREQUISITE_UNCHANGED") {
      return `PREREQUISITE_UNCHANGED: step ${stepId} of task ${taskId} is blocked by the unresolved prerequisite ${String(gate.failure_code)} ` +
        `and neither the commit nor the declared inputs changed since. It is recorded blocked, not retried. Resolve the prerequisite ` +
        `(a new commit or changed inputs) or leave the task PARTIAL or BLOCKED; other tasks may continue.`;
    }
    return `REPEATED_INEFFECTIVE_ACTION: step ${stepId} of task ${taskId} failed ${String(gate.count)} times with ${String(gate.failure_code)} ` +
      `at the same commit and inputs (retry budget ${String(gate.retry_budget)}; signature ${String(gate.signature).slice(0, 12)}). ` +
      `The step is recorded blocked. Change something first (a new commit, different inputs, or escalate the route) or leave the task PARTIAL or BLOCKED; other tasks may continue.`;
  }
  /** The structured failure code a worker result carries (its own blocker_code), else WORKER_FAILED. */
  function failureCode(details: Json): string {
    return typeof details.blocker_code === "string" && FAILURE_CODE.test(details.blocker_code) ? details.blocker_code : "WORKER_FAILED";
  }
  /**
   * Record a failed worker (not an abort) as a failure signature, or a result that declares its own
   * blocker_kind: "prerequisite". Only structured fields are read; the record classifies the code
   * (tools/parent_orchestration.py is_prerequisite). Free text is never parsed.
   */
  async function recordWorkerFailure(worker: { taskId: string | null; stepId: string | null; phase?: string },
                                     isError: boolean, details: Json, code: string): Promise<void> {
    if (!runId || !worker.taskId || !worker.stepId || !worker.phase) return;
    const declared = details.blocker_kind === "prerequisite";
    if (isError ? details.error === "ABORTED" : !(declared && typeof details.blocker_code === "string")) return;
    const head = await headOf(worker.taskId, worker.stepId);
    const digest = inputsDigest();
    await observe({ kind: "failure", phase: worker.phase, step_id: worker.stepId, code,
      ...(declared ? { blocker_kind: "prerequisite" } : {}),
      ...(head ? { head } : {}), ...(digest ? { inputs_digest: digest } : {}) });
  }
  /** Warn once at the warning threshold; at the limit, restrict tools to status and finish. */
  async function crossThresholds(): Promise<void> {
    const used = leadLimit;
    if (!used) return;
    if (used.warnAt !== null && used.calls >= used.warnAt && !used.warned) {
      used.warned = true;
      await observe({ kind: "lead_call_warning", calls: used.calls });
      pi.sendMessage?.({ customType: "lead-call-warning", display: true,
        content: `LEAD_CALL_WARNING: ${used.calls} of ${used.max} lead calls are used. Converge now: wrap up the current work, integrate finished tasks and record outcomes. ` +
          `At ${used.max} calls only orchestration_record status or finish stays available.` }, { deliverAs: "steer" });
    }
    if (used.calls >= used.max && !used.reached) {
      used.reached = true;
      await observe({ kind: "lead_limit", state: "reached", calls: used.calls });
    }
  }
  const leadGraceLeft = () => leadLimit ? Math.max(0, leadLimit.max + LEAD_CALL_GRACE - leadLimit.calls) : 0;
  /** The lead has used its calls; it may still read status and finish. */
  function leadLimitBlock(): string {
    const used = leadLimit!;
    return `LEAD_CALL_LIMIT: ${used.calls} of ${used.max} lead calls are used. Only orchestration_record status or finish is available now. ` +
      `Call orchestration_record {"action":"finish"} immediately with an explicit outcome (COMPLETED, PARTIAL, BLOCKED or FAILED); ` +
      `${leadGraceLeft()} more model call(s) are allowed before the run is stopped for an operator to resume with a higher limit.`;
  }
  /**
   * The lead did not finish within the grace. Fail closed but resumable: record the stop (the recorder writes a fresh
   * resume checkpoint and keeps the run IN_PROGRESS, stopped_reason LEAD_CALL_LIMIT), account for workers still in
   * flight, release the lease through the owner path, then stop the agent. The run is NOT finished.
   */
  async function stopAtLeadLimit(): Promise<void> {
    const id = runId;
    if (!id || !leadLimit) return;
    const used = leadLimit;
    for (const worker of inFlight.values()) {
      if (worker.taskId && worker.stepId) await observe({ kind: "worker", phase: worker.phase ?? worker.taskId,
        step_id: worker.stepId, status: "blocked", blocker_code: "LEAD_CALL_LIMIT", only_if_open: true });
    }
    for (const controller of workerAborts.values()) controller.abort();
    // Let aborted workers settle (bounded) before the checkpoint is taken and the lease is released, so no
    // worktree changes afterwards without anyone holding the lease.
    const settleBy = Date.now() + LEAD_STOP_SETTLE_MS;
    while (workerAborts.size > 0 && Date.now() < settleBy) await new Promise((resolve) => setTimeout(resolve, 25));
    workerAborts.clear();
    inFlight.clear();
    const stopped = await command(PARENT, ["event", id, ...ownerArgs()], { kind: "lead_call_limit_reached", calls: used.calls });
    if (runId !== id) return;
    if (stopped.ok === false) {
      // No fresh checkpoint: the run cannot be left resumable. Stay closed; the normal settle path ends it.
      if (LEASE_LOST.has(String(stopped.error_code))) { detach(stopped); return; }
      diagnose("lead_limit_stop", stopped);
      parentFailure = stopped; parentBlocked = true; blockAllWorktrees();
      lastCtx?.abort?.();
      return;
    }
    stopHeartbeat();
    const released = await command(LEASE, ["release", id, ...ownerArgs()]);
    if (released.ok === false) diagnose("lead_limit_release", released);
    runId = null;
    lastRunId = id;
    blockAllWorktrees();
    limitStopped = `LEAD_CALL_LIMIT: the run was stopped after ${used.calls} lead calls (limit ${used.max}) and left in progress for an operator to resume with a higher limit; nothing more can be recorded or run`;
    lastCtx?.abort?.();
  }
  async function scopeCheck(target: string, worker: string): Promise<Json> {
    const scope = await command(SESSION_SCOPE, [], { target_cwd: target, worker_cwd: worker });
    // Outside any Git repository, only the session's own directory is in scope.
    if (scope.ok !== true && scope.blocker_code === "SESSION_TARGET_UNRESOLVED" && samePath(target, worker)) {
      return { ok: true };
    }
    return scope;
  }
  async function snapshot(): Promise<{ digest: string; status: string[] } | null> {
    if (!targetCwd) return null;
    const result = await command(SESSION_SCOPE, [], { operation: "snapshot", path: targetCwd });
    return result.ok === true && typeof result.digest === "string"
      ? { digest: result.digest, status: Array.isArray(result.status) ? result.status as string[] : [] } : null;
  }

  /**
   * Resolve the routed task and step a worker call belongs to. An explicit
   * attribution (label or prompt tag) is required once more than one task is
   * routed; with exactly one approval an untagged call resolves to it as long
   * as its step is unambiguous for the tool's family.
   */
  type Resolution = { approval: RouteApproval | null; step: StepApproval | null } | { block: string };
  function resolveWorker(toolName: string, input: Record<string, unknown>): Resolution {
    if (toolName === "message_subagent") {
      // A continuation keeps the spawn's model and thinking; attribute it for wave accounting only.
      const tag = typeof input.subagent_id === "string" ? subagentTasks.get(input.subagent_id) : undefined;
      const approval = tag ? approvals.get(tag.taskId) ?? null
        : approvals.size === 1 ? [...approvals.values()][0] : null;
      return { approval, step: tag && approval ? approval.steps.get(tag.stepId) ?? null : null };
    }
    const family = CLAUDE_TOOLS.has(toolName) ? "claude" : "codex";
    const tag = workerAttribution(toolName, input);
    const convention = CLAUDE_TOOLS.has(toolName) ? "start the prompt with [pico-task <task_id>:<step_id>]"
      : "pass label \"<task_id>:<step_id>\"";
    // ClaudeWorker has no implicit single-task fallback: it always names its step.
    if (!tag && toolName === "ClaudeWorker") return { block: `WORKER_TASK_UNATTRIBUTED: ClaudeWorker prompts ${convention}` };
    if (tag) {
      const approval = approvals.get(tag.taskId);
      if (!approval) return { block: `WORKER_TASK_UNROUTED: task ${tag.taskId} has no successful route_task approval in this run` };
      const step = approval.steps.get(tag.stepId);
      if (!step) return { block: `WORKER_STEP_UNKNOWN: the routed plan of task ${tag.taskId} has no step ${tag.stepId}` };
      if (step.family !== family) {
        return { block: `MODEL_BACKEND_MISMATCH: step ${tag.stepId} of task ${tag.taskId} is a ${step.family} step` };
      }
      return { approval, step };
    }
    if (approvals.size === 0) return { approval: null, step: null };
    if (approvals.size > 1) {
      return { block: `WORKER_TASK_UNATTRIBUTED: several tasks are routed; ${convention} so the worker resolves to its task's approval` };
    }
    const approval = [...approvals.values()][0];
    const candidates = [...approval.steps.values()].filter((step) => step.family === family);
    if (candidates.length === 0) {
      return { block: `MODEL_BACKEND_MISMATCH: the routed plan of task ${approval.taskId} has no ${family} step` };
    }
    const [first] = candidates;
    if (candidates.every((step) => step.model === first.model && step.effort === first.effort && step.mode === first.mode)) {
      return { approval, step: first };
    }
    return { block: `WORKER_STEP_UNATTRIBUTED: task ${approval.taskId} has several ${family} steps with different assignments; ${convention}` };
  }

  /**
   * Always-on session isolation, also between parent runs: a follow-up turn
   * after /orchestrate must not resume a shared Claude session, hand Claude the
   * Pi conversation, or continue a subagent from another repository.
   */
  async function guardOutsideRun(call: { toolName: string; input: Record<string, unknown> }, cwd: string): Promise<Guard> {
    if (call.toolName === "ClaudeWorker") {
      return { block: true, reason: "WORKER_TASK_UNROUTED: ClaudeWorker runs only for a routed Claude step of an active /orchestrate run" };
    }
    if (call.toolName === "AskClaude") {
      if (call.input.isolated === false) {
        return { block: true, reason: "SESSION_ISOLATION_REQUIRED: AskClaude runs isolated while the orchestrator is loaded; pass state through explicit artifacts" };
      }
      const scope = await scopeCheck(cwd, process.cwd());
      if (scope.ok !== true) {
        return { block: true, reason: `${String(scope.blocker_code ?? "SESSION_REPOSITORY_MISMATCH")}: the Claude worker directory is outside this session's repository` };
      }
      call.input.isolated = true;
    }
    if (call.toolName === "message_subagent") {
      const id = call.input.subagent_id;
      const origin = typeof id === "string" ? spawnedSubagents.get(id) : undefined;
      if (!origin) {
        return { block: true, reason: "SESSION_SCOPE_UNVERIFIED: this subagent was not spawned in the current Pi process; spawn a fresh worker and pass state through explicit artifacts" };
      }
      const scope = await scopeCheck(cwd, origin);
      if (scope.ok !== true) {
        return { block: true, reason: "SESSION_REPOSITORY_MISMATCH: this subagent was spawned in another repository" };
      }
    }
    return undefined;
  }
  async function settle(): Promise<void> {
    if (!runId) return;
    const result = await command(PARENT, ["settle", runId, ...ownerArgs()]);
    if (result.ok === false) {
      diagnose("settle", result);
      parentBlocked = true;
      parentFailure = result;
      return;
    }
    lastTerminal = result;
    persist(false);
    runId = null;
    stopHeartbeat();
    parentBlocked = false;
    parentFailure = null;
    runSubagents.clear();
    workerStarts.clear();
    budget = null;
    recordedPeak = 0;
    compactionFailed = false;
    resetRunState();
  }

  /** Close the concurrency accounting of a worker; emits telemetry when the last one in flight returns. */
  async function workerFinished(toolCallId: string): Promise<void> {
    if (!inFlight.delete(toolCallId) || inFlight.size > 0) return;
    const activeMs = Date.now() - batchStart;
    const peak = batchPeak;
    batchPeak = 0;
    if (runId && !parentBlocked) await observe({ kind: "concurrency", peak_workers: peak, active_ms: activeMs });
  }

  pi.on("session_start", async (_event, ctx) => { await restore(ctx); });

  pi.on("before_agent_start", async (trigger, ctx) => {
    lastCtx = ctx;
    compactionFailed = false;
    // Pi >= 1.3 only carries `prompt` on runs started by a user prompt.
    if (!("prompt" in trigger) || !trigger.prompt.includes(MARKER)) return;
    if (parentBlocked && !runId) {
      const code = String(parentFailure?.error_code ?? "PARENT_PROVENANCE_UNAVAILABLE");
      return { message: { customType: "orchestration-blocker", display: true,
        content: `BLOCKED: parent orchestration continuation failed (${code}). Start a new Pi session after resolving the repository mismatch.` } };
    }
    limitStopped = null;
    await settle();
    if (runId) {
      return { message: { customType: "orchestration-blocker", display: true,
        content: "BLOCKED: the previous parent run could not be settled. Resolve the provenance error before starting another run." } };
    }
    const args = ["start", "--worktree", ctx.cwd];
    targetCwd = ctx.cwd;
    const explicit = process.env.PI_TARGET_REPOSITORY;
    if (explicit) args.push("--repo", explicit);
    args.push(...leaseArgs());
    const result = await command(PARENT, args);
    runId = typeof result.run_id === "string" ? result.run_id : null;
    if (runId) { lastRunId = runId; lastTerminal = null; persist(true); leaseGranted(result); }
    resetRunState();
    parentBlocked = !runId || result.status !== "IN_PROGRESS";
    parentFailure = parentBlocked ? result : null;
    if (parentBlocked) {
      diagnose("start", result);
      const code = typeof result.blocker_code === "string" ? result.blocker_code : "PARENT_PROVENANCE_UNAVAILABLE";
      ctx.ui.notify(`Parent orchestration blocked: ${code}`, "error");
      return { message: { customType: "orchestration-blocker", display: true,
        content: `BLOCKED: parent orchestration initialization failed (${code}). Stop before repository inspection or workers. See orchestration-lifecycle diagnostic log.` } };
    }
    const started = await command(CONTEXT, ["start", runId!, ...ownerArgs()], { cwd: ctx.cwd });
    if (started.ok === false) diagnose("context_start", started);
    await configureLeadLimit();
    await configureFailureBudget();
    budget = initialBudgetState();
    startHeartbeat(result);
  });

  type ClaudeRoute = { block: string } | { approval: RouteApproval; step: StepApproval; backend: Json;
    canonical: string; backendModel: string; effort: string };
  /**
   * Checks shared by routed Claude calls: the preflighted executable is
   * unchanged, and model, effort, and mode are the routed step's. `model` may be
   * omitted only where the tool resolves it from the route (ClaudeWorker).
   */
  async function claudeRoute(input: Record<string, unknown>, approval: RouteApproval | null,
                             step: StepApproval | null, modelRequired: boolean): Promise<ClaudeRoute> {
    const backend = approval?.claudeBackend ?? null;
    if (!approval || !backend || !targetCwd) return { block: "MODEL_BACKEND_MISMATCH: no preflighted Claude backend" };
    const verification = await command(PREFLIGHT, [], { verify_backend: true,
      backend_identity: backend, cwd: targetCwd });
    if (verification.ok !== true) {
      approval.worktreeReady = false;
      return { block: "MODEL_BACKEND_MISMATCH: Claude bridge runtime changed since preflight" };
    }
    const canonical = input.model === undefined && !modelRequired ? step?.model : input.model;
    if (typeof canonical !== "string") {
      return { block: "MODEL_IDENTIFIER_UNSUPPORTED: routed Claude calls require an explicit canonical model" };
    }
    const mapping = resolveClaudeBackendModel(canonical);
    if (!mapping.ok) return { block: `${mapping.blocker_code}: no safe Claude Code model invocation` };
    if (!step || canonical !== step.model) {
      return { block: "MODEL_BACKEND_MISMATCH: Claude model was not approved by the routed preflight of this task" };
    }
    if (input.isolated === false) {
      return { block: "SESSION_ISOLATION_REQUIRED: routed Claude workers run isolated; pass state through explicit artifacts, not conversation history" };
    }
    if (!step.effort) {
      return { block: "REASONING_EFFORT_UNRESOLVED: the routed result assigns no reasoning effort to the Claude step" };
    }
    if (input.thinking !== undefined && input.thinking !== step.effort) {
      return { block: `REASONING_EFFORT_MISMATCH: the routed Claude step is assigned thinking=${step.effort}` };
    }
    if (input.mode !== undefined && input.mode !== step.mode) {
      return { block: `MUTATION_MODE_MISMATCH: the routed Claude step runs with mode=${step.mode}` };
    }
    return { approval, step, backend, canonical, backendModel: mapping.backend_model, effort: step.effort };
  }

  /**
   * Checks for a routed AskClaude call; returns a block reason or rewrites the call in place.
   * The bridge runs Claude Code in the Pi process directory, so only read-only
   * steps may use it; mutating steps run through ClaudeWorker.
   */
  async function guardAskClaude(call: { toolCallId: string; input: Record<string, unknown> },
                                approval: RouteApproval | null, step: StepApproval | null): Promise<string | undefined> {
    const route = await claudeRoute(call.input, approval, step, true);
    if ("block" in route) return route.block;
    if (route.step.mode === "full") {
      return `CLAUDE_WORKER_REQUIRED: mutating Claude step ${route.step.id} of task ${route.approval.taskId} runs through ClaudeWorker in its bound worktree; call ClaudeWorker with the same [pico-task ${route.approval.taskId}:${route.step.id}] prompt`;
    }
    const scope = await scopeCheck(targetCwd!, process.cwd());
    if (scope.ok !== true) {
      const code = typeof scope.blocker_code === "string" ? scope.blocker_code : "SESSION_REPOSITORY_MISMATCH";
      return `${code}: the Claude worker directory is outside the parent run's target repository`;
    }
    call.input.isolated = true;
    call.input.thinking = route.effort;
    call.input.mode = route.step.mode;
    call.input.model = route.backendModel;
    claudeCalls.set(call.toolCallId, { canonical: route.canonical, backend: route.backendModel });
    return undefined;
  }

  /**
   * Checks for a routed ClaudeWorker call and resolves its run: the pinned
   * executable, the bound worktree as working directory (read-only steps fall
   * back to the target checkout), and the routed model and effort.
   */
  async function guardClaudeWorker(call: { toolCallId: string; input: Record<string, unknown> },
                                   approval: RouteApproval | null, step: StepApproval | null): Promise<string | undefined> {
    const route = await claudeRoute(call.input, approval, step, false);
    if ("block" in route) return route.block;
    const { taskId } = route.approval;
    const workspace = workspaces.get(workspaceKey(taskId, route.step.id));
    if (route.step.mode === "full" && !workspace) {
      return `WORKER_WORKSPACE_UNBOUND: bind the prepared worktree first with orchestration_record {"action":"bind_workspace","task_id":"${taskId}","step_id":"${route.step.id}","path":...}`;
    }
    if (route.step.mode === "full" && workspace && inGitDirectory(workspace)) {
      return `WORKER_WORKSPACE_PROTECTED_PATH: bound worktree ${workspace} is inside a .git directory, where Claude Code denies every edit; create the worktree outside .git and bind it again`;
    }
    const cwd = workspace ?? targetCwd!;
    const scope = await scopeCheck(targetCwd!, cwd);
    if (scope.ok !== true) {
      const code = typeof scope.blocker_code === "string" ? scope.blocker_code : "SESSION_REPOSITORY_MISMATCH";
      return `${code}: the Claude worker directory is outside the parent run's target repository`;
    }
    // The preflight-pinned file, never a fresh PATH lookup: verify_backend just matched its identity.
    const executable = route.backend.backend_executable;
    if (typeof executable !== "string" || !executable) {
      return "MODEL_BACKEND_MISMATCH: the preflighted Claude identity names no executable";
    }
    call.input.model = route.canonical;
    call.input.thinking = route.effort;
    workerPlans.set(call.toolCallId, { executable, cwd, backendModel: route.backendModel, effort: route.effort,
      mode: route.step.mode, taskId, stepId: route.step.id, phase: route.approval.phase, canonical: route.canonical });
    return undefined;
  }

  /** Checks for a routed spawn_subagent call: the Codex model and thinking come from the route. */
  function guardCodex(input: Record<string, unknown>, approval: RouteApproval | null,
                      step: StepApproval | null): string | undefined {
    if (!approval || !step) return undefined; // unrouted exploration before any route_task
    if (!step.model) {
      return `MODEL_ASSIGNMENT_UNRESOLVED: the routed result binds no model to step ${step.id} of task ${approval.taskId}`;
    }
    if (!step.effort) {
      return `REASONING_EFFORT_UNRESOLVED: the routed result assigns no reasoning effort to step ${step.id}`;
    }
    if (input.model !== undefined && input.model !== step.model) {
      return `MODEL_BACKEND_MISMATCH: routed Codex step ${step.id} is assigned model=${step.model}`;
    }
    if (input.thinking !== undefined && input.thinking !== step.effort) {
      return `REASONING_EFFORT_MISMATCH: routed Codex step ${step.id} is assigned thinking=${step.effort}`;
    }
    if (step.mode === "full") {
      const workspace = workspaces.get(workspaceKey(approval.taskId, step.id));
      if (!workspace) {
        return `WORKER_WORKSPACE_UNBOUND: bind the prepared worktree first with orchestration_record {"action":"bind_workspace","task_id":"${approval.taskId}","step_id":"${step.id}","path":...}`;
      }
      if (typeof input.task !== "string" || !input.task.includes(workspace)) {
        return `WORKER_WORKSPACE_UNSTATED: the mutating Codex worker contract must name its bound worktree ${workspace}`;
      }
    }
    input.model = step.model;
    input.thinking = step.effort;
    if (input.label === undefined) input.label = `${approval.taskId}:${step.id}`;
    return undefined;
  }

  pi.on("tool_call", async (call, ctx) => {
    lastCtx = ctx;
    // No successful renewal for most of the lease's lifetime: stop before another owner may take it.
    if (leaseLapsed()) selfExpire();
    if (limitStopped && !(call.toolName === "orchestration_record" && call.input.action === "status")) {
      return { block: true, terminate: true, reason: limitStopped };
    }
    if (runId && leadLimit?.reached && !(call.toolName === "orchestration_record"
        && (call.input.action === "status" || call.input.action === "finish"))) {
      return { block: true, reason: leadLimitBlock() };
    }
    if (!runId && !parentBlocked) return guardOutsideRun(call, ctx.cwd);
    if (parentBlocked && call.toolName !== "orchestration_record") {
      return { block: true, terminate: true,
        reason: "Parent orchestration provenance is blocked; only orchestration_record status is available" };
    }
    if (pendingReroutes.size > 0 && (WORKER_TOOLS.has(call.toolName) || isWorktreeAdd(call))) {
      return { block: true,
        reason: `PARENT_ROUTE_RECHECK_REQUIRED: rerun route_task and backend preflight for ${[...pendingReroutes].sort().join(", ")} before worker dispatch` };
    }
    if (scopeViolation && (WORKER_TOOLS.has(call.toolName) || isWorktreeAdd(call))) {
      return { block: true, reason: `WORKER_SCOPE_VIOLATION: a worker changed the parent checkout (${scopeViolation}); review those changes before launching more workers` };
    }
    if (integrating && WORKER_TOOLS.has(call.toolName)) {
      return { block: true, reason: `INTEGRATION_IN_PROGRESS: wave ${integrating} is integrating; finish with orchestration_record {"action":"integrate_end"} before launching workers` };
    }
    if (call.toolName === "message_subagent") {
      const id = call.input.subagent_id;
      if (typeof id !== "string" || !runSubagents.has(id)) {
        return { block: true, reason: "SESSION_SCOPE_VIOLATION: only subagents spawned in this parent run may be continued; spawn a fresh worker and pass state through explicit artifacts" };
      }
    }
    if (WORKER_TOOLS.has(call.toolName)) {
      const resolved = resolveWorker(call.toolName, call.input);
      if ("block" in resolved) return { block: true, reason: resolved.block };
      const { approval, step } = resolved;
      const taskId = approval?.taskId ?? null;
      // Workers of different tasks may overlap only inside one declared wave.
      const others = [...inFlight.values()].filter((worker) => worker.taskId !== taskId);
      if (others.length > 0) {
        const wave = activeWave;
        const inWave = (task: string | null) => task !== null && wave !== null && wave.tasks.has(task);
        if (!inWave(taskId) || !others.every((worker) => inWave(worker.taskId))) {
          return { block: true, reason: "WAVE_UNDECLARED: workers of different tasks run concurrently only when every task is in the declared wave; call orchestration_record declare_wave first" };
        }
      }
      if (call.toolName === "AskClaude") {
        const reason = await guardAskClaude(call, approval, step);
        if (reason) return { block: true, reason };
      } else if (call.toolName === "ClaudeWorker") {
        const reason = await guardClaudeWorker(call, approval, step);
        if (reason) return { block: true, reason };
      } else if (call.toolName === "spawn_subagent") {
        const reason = guardCodex(call.input, approval, step);
        if (reason) return { block: true, reason };
      }
      // A step whose failure signature used up its retry budget is refused here, before any state is taken.
      if (approval && step && (call.toolName === "ClaudeWorker" || call.toolName === "spawn_subagent")) {
        const refusal = await retryGate(approval.taskId, approval.phase, step.id);
        if (refusal) {
          claudeCalls.delete(call.toolCallId);
          workerPlans.delete(call.toolCallId);
          return { block: true, reason: refusal };
        }
      }
      // Workers must never write to the parent checkout: compare it before and after.
      const before = await snapshot();
      if (!before) {
        claudeCalls.delete(call.toolCallId);
        workerPlans.delete(call.toolCallId);
        return { block: true, reason: "WORKER_SCOPE_UNVERIFIABLE: could not snapshot the parent checkout" };
      }
      snapshots.set(call.toolCallId, before);
      // A routed task started while no worker runs and no wave is declared is a single-task wave: record it
      // here so the lead need not. Concurrent work still needs the lead's declare_wave (gate above).
      // Only a mutating step starts one, and only when the task has no open wave in its current route epoch:
      // a read-only step (specifier, reviewer) never adds a wave, so after integration the phase keeps pointing
      // at the integrated wave. A task that mutates again after integration starts a new one.
      if (taskId && approval && step?.mode === "full" && inFlight.size === 0 && activeWave === null &&
          ![...implicitOpen].some(([id, members]) => members.includes(taskId)
            && implicitEpoch.get(id) === (routeEpochs.get(taskId) ?? 0))) await recordImplicitWave(taskId);
      // A mutating step that starts in an already bound worktree re-marks the task as unintegrated, so work done
      // after an earlier integration (no new bind) can never leave COMPLETED passing without integration.
      if (runId && taskId && approval && step?.mode === "full") {
        await observe({ kind: "checkpoint", phase: approval.phase, reason: "worker_approval", workspace_bound: true });
      }
      if (inFlight.size === 0) batchStart = Date.now();
      inFlight.set(call.toolCallId, { taskId, stepId: step?.id ?? null, tool: call.toolName,
        phase: workerPlans.get(call.toolCallId)?.phase ?? (call.toolName === "spawn_subagent" && step ? approval?.phase : undefined) });
      // The activity entry carries what a lead directive event used to: task, step, route, and a digest of
      // the worker contract (never its text).
      const fields = call.input as Record<string, unknown>;
      const contract = [fields.prompt, fields.task, fields.message]
        .find((value): value is string => typeof value === "string" && value.length > 0);
      const parts = contract ? contractParts(contract) : null;
      if (runId) await observe({ kind: "activity", status: "started", tool: call.toolName,
        task_id: taskId, step_id: step?.id ?? null,
        ...(approval ? { route: approval.route } : {}),
        ...(contract && parts ? { prompt_digest: createHash("sha256").update(contract).digest("hex"),
                                  ...(parts.excerpt ? { prompt_excerpt: rawWindow(parts.excerpt, EXCERPT_CHARS) } : {}),
                                  ...(parts.goal ? { goal: rawWindow(parts.goal, GOAL_CHARS) } : {}) } : {}) });
      batchPeak = Math.max(batchPeak, inFlight.size);
      workerStarts.set(call.toolCallId, Date.now());
      const plan = workerPlans.get(call.toolCallId);
      // A routed Codex step: the validated model and reasoning effort are recorded here, so the record names
      // which model ran even when the lead records nothing. The lead's own worker event still wins.
      if (call.toolName === "spawn_subagent" && approval && step?.model) {
        autoWorkers.add(workspaceKey(approval.phase, step.id));
        await observe({ kind: "worker", phase: approval.phase, step_id: step.id, status: "in_progress", only_if_open: true, model: step.model,
          ...(step.effort ? { reasoning_effort: step.effort } : {}) });
      }
      // Observational: the lead may record the worker itself; the tool_result handler records its terminal status.
      if (plan) autoWorkers.add(workspaceKey(plan.phase, plan.stepId));
      if (plan) await observe({ kind: "worker", phase: plan.phase, step_id: plan.stepId, status: "in_progress", only_if_open: true,
        model: plan.canonical, backend: "claude-code", backend_model: plan.backendModel, reasoning_effort: plan.effort });
    }
    if (call.toolName === "route_task") {
      const task = (call.input.classified_task ?? {}) as Json;
      if (typeof task.id === "string") {
        // Re-routing a task replaces only that task's approval.
        approvals.delete(task.id);
        pendingRoutes.set(call.toolCallId, task.id);
      }
    }
    if (isWorktreeAdd(call) && !worktreeReady()) {
      return { block: true, reason: "Implementation worktree requires successful routed backend preflight" };
    }
    const backend = anyClaudeBackend();
    if (isWorktreeAdd(call) && backend && targetCwd) {
      const verification = await command(PREFLIGHT, [], { verify_backend: true,
        backend_identity: backend, cwd: targetCwd });
      if (verification.ok !== true) {
        blockAllWorktrees();
        return { block: true, reason: "MODEL_BACKEND_MISMATCH: Claude bridge runtime changed since preflight" };
      }
    }
  });

  pi.on("tool_result", async (result, ctx) => {
    const worker = inFlight.get(result.toolCallId);
    if (result.toolName === "spawn_subagent" && !result.isError) {
      const id = ((result.details ?? {}) as Json).subagent_id;
      if (typeof id === "string") {
        spawnedSubagents.set(id, ctx.cwd);
        if (worker?.taskId && worker.stepId) subagentTasks.set(id, { taskId: worker.taskId, stepId: worker.stepId });
        if (runId && !runSubagents.has(id)) {
          await observe({ kind: "spawn" });
          await observe({ kind: "activity", status: "spawned", tool: "spawn_subagent",
            task_id: worker?.taskId ?? null, step_id: worker?.stepId ?? null, subagent_id: id });
        }
      }
    }
    await workerFinished(result.toolCallId);
    if (runId && worker) {
      // What the worker returned, bounded: the first part of its final text, a digest of all of it, and the
      // commit it reports. This replaces the lead's feedback events; the full text stays in the Pi transcript.
      const text = result.content.filter((item) => item.type === "text").map((item) => item.text).join("\n");
      const commit = reportedCommit(text);
      await observe({ kind: "activity", status: result.isError ? "failed" : "finished",
        tool: worker.tool, task_id: worker.taskId, step_id: worker.stepId,
        ...(text ? { result_summary: rawWindow(text, SUMMARY_CHARS),
                     result_digest: createHash("sha256").update(text).digest("hex") } : {}),
        ...(commit ? { commit_sha: commit } : {}) });
    }
    const before = snapshots.get(result.toolCallId);
    snapshots.delete(result.toolCallId);
    if (runId && before) {
      const after = await snapshot();
      if (!after || after.digest !== before.digest) {
        const changed = after ? after.status.filter((entry) => !before.status.includes(entry)) : [];
        scopeViolation = changed.slice(0, 5).join(", ") || "unattributed changes";
        blockAllWorktrees();
        const phase = (worker?.taskId && approvals.get(worker.taskId)?.phase) || lastPhase;
        await event({ kind: "phase", phase, status: "blocked", blocker_code: "WORKER_SCOPE_VIOLATION" });
        claudeCalls.delete(result.toolCallId);
        return { content: [...result.content, { type: "text" as const,
          text: `Orchestration blocker: WORKER_SCOPE_VIOLATION. The parent checkout changed during this worker call (${scopeViolation}). Workers may only write in their bound worktree. Nothing was reverted; inspect the changes before continuing.` }] };
      }
    }
    if (runId && !parentBlocked && WORKER_TOOLS.has(result.toolName)) {
      const started = workerStarts.get(result.toolCallId);
      workerStarts.delete(result.toolCallId);
      const details = (result.details ?? {}) as Json;
      if (result.toolName === "AskClaude") {
        if (claudeCalls.has(result.toolCallId)) {
          await observe({ kind: "usage", backend: "claude-code",
            duration_ms: count(details.executionTime) ?? (started ? Date.now() - started : null) });
        }
      } else if (result.toolName === "ClaudeWorker") {
        // Only a run the guard resolved reports usage; a blocked call never started Claude Code.
        if (typeof details.task_id === "string") {
          const usage = (details.usage ?? {}) as Json;
          const cost = usage.cost_usd;
          await observe({ kind: "usage", backend: "claude-code",
            duration_ms: count(details.elapsed_ms) ?? (started ? Date.now() - started : null),
            input_tokens: count(usage.input_tokens), output_tokens: count(usage.output_tokens),
            cached_tokens: count(usage.cached_tokens), total_tokens: count(usage.total_tokens),
            ...(count(usage.cache_write_tokens) === null ? {} : { cache_write_tokens: count(usage.cache_write_tokens) }),
            ...(typeof cost === "number" && Number.isFinite(cost) && cost >= 0 ? { estimated_cost_usd: cost } : {}) });
          // The worker's own terminal status, so a lead that never records it leaves nothing in_progress.
          // An abort (lease loss, user stop) is blocked; any other failure is failed. `only_if_open` makes this
          // the raw result only: it never overrides a status the lead already recorded, and the lead may later
          // record a different judgment (completed -> failed/blocked with a blocker code) or add its own code.
          if (worker?.phase && worker.stepId) {
            const code = failureCode(details);
            await recordWorkerFailure(worker, result.isError === true, details, code);
            await observe({ kind: "worker", phase: worker.phase, step_id: worker.stepId, only_if_open: true,
              ...(result.isError ? { status: details.error === "ABORTED" ? "blocked" : "failed", blocker_code: code }
                                 : { status: "completed" }) });
          }
        }
      } else {
        if (result.toolName === "spawn_subagent" && typeof details.subagent_id === "string" && !result.isError) {
          runSubagents.add(details.subagent_id);
        }
        // The worker's own terminal status, like ClaudeWorker's: it never overrides one the lead recorded.
        if (worker?.phase && worker.stepId && result.toolName === "spawn_subagent") {
          const code = failureCode(details);
          await recordWorkerFailure(worker, result.isError === true, details, code);
          await observe({ kind: "worker", phase: worker.phase, step_id: worker.stepId, only_if_open: true,
            ...(result.isError ? { status: "failed", blocker_code: code } : { status: "completed" }) });
        }
        const usage = (details.usage ?? {}) as Json;
        await observe({ kind: "usage", backend: "pi-subagent",
          duration_ms: count(details.elapsedMs) ?? (started ? Date.now() - started : null),
          input_tokens: count(usage.input), output_tokens: count(usage.output),
          cached_tokens: count(usage.cacheRead), total_tokens: count(usage.totalTokens),
          ...(count(usage.cacheWrite) === null ? {} : { cache_write_tokens: count(usage.cacheWrite) }) });
      }
    }
    if (result.toolName === "AskClaude") {
      const models = claudeCalls.get(result.toolCallId);
      claudeCalls.delete(result.toolCallId);
      if (!models) return;
      const details = result.details as { error?: boolean } | undefined;
      if (details?.error === true || result.isError) {
        const message = result.content.filter((item) => item.type === "text").map((item) => item.text).join(" ");
        const code = classifyClaudeInvocationError(message);
        return { content: [...result.content, { type: "text" as const,
          text: `Orchestration blocker: ${code}. Canonical model: ${models?.canonical ?? "unknown"}; Claude Code model: ${models?.backend ?? "unknown"}. Do not infer entitlement from an ambiguous backend error.` }] };
      }
      return;
    }
    if (!runId || parentBlocked || result.toolName !== "route_task") return;
    const pending = pendingRoutes.get(result.toolCallId);
    pendingRoutes.delete(result.toolCallId);
    const details = result.details as { result?: Json; blocker_code?: unknown } | undefined;
    const route = details?.result;
    if (!route || route.ok !== true || details?.blocker_code !== undefined) {
      if (pending) approvals.delete(pending);
      const reported = typeof details?.blocker_code === "string" ? details.blocker_code
        : typeof route?.blocker_code === "string" ? route.blocker_code : "ROUTING_FAILED";
      const code = /^[A-Z][A-Z0-9_]{0,99}$/.test(reported) ? reported : "ROUTING_FAILED";
      await event({ kind: "phase", phase: "routing", status: "blocked", blocker_code: code });
      return;
    }
    const classification = route.classification as { base?: { id?: string }; effective?: { task_type?: string } } | undefined;
    const phase = classification?.base?.id ?? pending ?? "routing";
    const taskId = phase;
    approvals.delete(taskId);
    lastPhase = phase;
    const summary = routingSummary(route);
    if (!await event({ kind: "phase", phase, status: "in_progress", route: route.selected_route,
                       policy_version: route.policy_version, ...(summary ? { routing: summary } : {}) })) {
      if (parentFailure?.error_code === "PARENT_EVENT_CONFLICT") pendingReroutes.add(taskId);
      return { content: [...result.content, { type: "text" as const,
        text: "Parent provenance update failed. Implementation is blocked." }] };
    }
    const check = await command(PREFLIGHT, [], {
      route_task_result: route,
      available_tools: pi.getActiveTools(),
      cwd: targetCwd,
    });
    if (check.ok !== true) {
      const code = typeof check.blocker_code === "string" ? check.blocker_code : "MODEL_BACKEND_UNAVAILABLE";
      await event({ kind: "phase", phase, status: "blocked", blocker_code: code });
      if (typeof check.step_id === "string") {
        await event({ kind: "worker", phase, step_id: check.step_id, model: check.model,
                      backend_model: check.backend_model ?? null,
                      status: "blocked", blocker_code: code,
                      installed_backend_version: check.installed_backend_version ?? null,
                      required_backend_version: check.required_backend_version ?? null });
      }
      return { content: [...result.content, { type: "text" as const,
        text: `Backend preflight BLOCKED: ${code}. No implementation worktree may be created.` }] };
    }
    const checked = (Array.isArray(check.checked_steps) ? check.checked_steps : []) as Json[];
    const claude = checked.find((step) => step.backend === "claude");
    const steps = new Map<string, StepApproval>();
    const plannedSteps = ((route.execution_plan as Json | undefined)?.steps ?? []) as Json[];
    for (const planned of plannedSteps) {
      if (typeof planned.id !== "string" || typeof planned.family !== "string") continue;
      const preflighted = checked.find((item) => item.step_id === planned.id);
      steps.set(planned.id, { id: planned.id, family: planned.family,
        model: typeof preflighted?.model === "string" ? preflighted.model : assignedModel(route, planned),
        effort: assignedEffort(route, planned.id),
        mode: planned.mutation_permission === "allowed" ? "full" : "read" });
    }
    approvals.set(taskId, { taskId, route: String(route.selected_route), phase, worktreeReady: !parentBlocked,
      taskType: typeof classification?.effective?.task_type === "string" ? classification.effective.task_type : null,
      claudeBackend: claude ? { backend_executable: claude.backend_executable,
        backend_version_output: claude.backend_version_output,
        backend_device: claude.backend_device, backend_inode: claude.backend_inode } : null,
      steps, result: route });
    pendingReroutes.delete(taskId);
    // Worker approval is a resume checkpoint: the parent checkout as approved.
    await event({ kind: "checkpoint", phase, reason: "worker_approval" });
    const claudeEffort = claude ? assignedEffort(route, claude.step_id) : null;
    if (claude) await event({ kind: "worker", phase, step_id: claude.step_id,
      status: "not_started", model: claude.model, backend: "claude-code",
      backend_model: claude.backend_model,
      ...(claudeEffort ? { reasoning_effort: claudeEffort } : {}),
      installed_backend_version: claude.installed_backend_version });
    return { content: [...result.content, { type: "text" as const,
      text: `Backend preflight READY for task ${taskId} with the exact routed models and tools. Model entitlement remains unverified until invocation. Attribute its workers as "${taskId}:<step_id>".` }] };
  });

  function reply(ok: boolean, text: string, result: Json = {}) {
    return { content: [{ type: "text" as const, text }],
             details: { ok, active: runId !== null, run_id: runId, result } };
  }

  /**
   * Record a single-task wave for a task the lead started without declaring one. Telemetry-grade: a refused
   * write is diagnosed and leaves no wave, so the previous behaviour (no wave) applies and nothing blocks.
   */
  async function recordImplicitWave(taskId: string): Promise<void> {
    if (!runId || parentBlocked) return;
    // One implicit wave per task: it is never grown, and it never lets tasks run concurrently (the gate counts
    // only an explicit declare_wave, which also checks write-scope overlap and the parallel limit).
    // Unique across processes (a resumed run has no counter), so a repeated task never reuses an integrated wave.
    const id = `auto-${randomBytes(4).toString("hex")}-${taskId}`.slice(0, 128);
    const result = await command(PARENT, ["event", runId, ...ownerArgs()],
      { kind: "wave", wave_id: id, task_ids: [taskId], status: "declared" });
    if (result.ok === false) { diagnose("telemetry", result); return; }
    implicitOpen.set(id, [taskId]);
    implicitEpoch.set(id, routeEpochs.get(taskId) ?? 0);
  }

  async function declareWave(waveId: string, tasks: { task_id: string; write_scope: string[] }[]) {
    if (integrating) return reply(false, `BLOCKED: INTEGRATION_IN_PROGRESS: wave ${integrating} is still integrating.`);
    if (inFlight.size > 0) return reply(false, "BLOCKED: WAVE_WORKERS_IN_FLIGHT: wait for running workers before declaring the next wave.");
    const ids = tasks.map((task) => task.task_id);
    if (ids.length === 0 || new Set(ids).size !== ids.length) {
      return reply(false, "BLOCKED: WAVE_SCOPE_INVALID: a wave lists one or more distinct task ids.");
    }
    const unsafe = tasks.flatMap((task) => task.write_scope.filter((entry) => scopePrefix(entry) === null));
    if (unsafe.length > 0) {
      return reply(false, `BLOCKED: WAVE_SCOPE_INVALID: write scopes are repository-relative paths or globs without "..": ${unsafe.slice(0, 3).join(", ")}`);
    }
    const unrouted = ids.filter((id) => !approvals.has(id));
    if (unrouted.length > 0) {
      return reply(false, `BLOCKED: WAVE_TASK_UNROUTED: route every wave task first (${unrouted.join(", ")}).`);
    }
    const limits = await command(AUTONOMY, ["limits", "--worktree", targetCwd ?? "."]);
    if (limits.ok === false) {
      return reply(false, `BLOCKED: ${String(limits.blocker_code ?? "AUTONOMY_POLICY_INVALID")}: .pi/autonomy.yaml could not be read.`, limits);
    }
    const limit = count(limits.parallel_tasks) || DEFAULT_PARALLEL_TASKS;
    if (tasks.length > limit) {
      return reply(false, `BLOCKED: WAVE_LIMIT_EXCEEDED: the wave has ${tasks.length} tasks; limits.parallel_tasks is ${limit}. Defer the rest to the next wave.`);
    }
    for (let left = 0; left < tasks.length; left += 1) {
      for (let right = left + 1; right < tasks.length; right += 1) {
        for (const a of tasks[left].write_scope) {
          const b = tasks[right].write_scope.find((entry) => scopesOverlap(a, entry));
          if (b !== undefined) {
            return reply(false, `BLOCKED: WAVE_SCOPE_OVERLAP: ${tasks[left].task_id} (${a}) and ${tasks[right].task_id} (${b}) may write the same paths; run them in different waves.`);
          }
        }
      }
    }
    if (!await event({ kind: "wave", wave_id: waveId, task_ids: ids, status: "declared" })) {
      return reply(false, "BLOCKED: parent provenance update failed; see diagnostic log.", parentFailure ?? {});
    }
    activeWave = { id: waveId, tasks: new Map(tasks.map((task) => [task.task_id, task.write_scope])) };
    // An implicit wave whose tasks all joined this wave is superseded; any other stays open and visible.
    for (const [id, members] of implicitOpen) {
      if (members.every((task) => ids.includes(task))) { implicitOpen.delete(id); implicitEpoch.delete(id); }
    }
    return reply(true, `Wave ${waveId} declared with ${ids.length} task(s) (limit ${limit}). Launch all their ready workers in one tool batch, each attributed "<task_id>:<step_id>".`,
      { wave_id: waveId, task_ids: ids, parallel_tasks: limit });
  }

  /**
   * Post-diff classification check, before a wave's worktrees are merged into the parent checkout: every worktree
   * bound for a member task is diffed and its real changed paths are matched against the path floors of the route
   * result that ran (tools/diff_floor_check.py; the decision is routing_engine.check_diff_path_floors). A task whose
   * diff needs an earlier route in the policy precedence, or a higher capacity on the same route, is not integrated:
   * the parent record gets a PATH_FLOOR re-route (a `diff_floors` event), and the task is re-routed here exactly like
   * an escalation, so the run stays active and resumable. Anything unverifiable refuses the integration. Returns the
   * refusal reply, or null when the wave may integrate.
   */
  async function diffFloorGate(members: string[]) {
    const underrated: string[] = [];
    for (const taskId of members) {
      const bound = [...new Set([...workspaces].filter(([key]) => key.startsWith(`${taskId}\u0000`))
        .map(([, path]) => path))].sort();
      if (bound.length === 0) continue; // no worktree was bound for the task, so no worker of it could write one
      const approval = approvals.get(taskId);
      if (!approval || !targetCwd) {
        return reply(false, `BLOCKED: ${DIFF_UNVERIFIABLE}: task ${taskId} has a bound worktree but no route result to check its diff against.`);
      }
      for (const path of bound) {
        const verdict = await command(DIFF_FLOORS, [],
          { route_task_result: approval.result, worktree: path, parent_checkout: targetCwd });
        const matches = Array.isArray(verdict.diff_path_floor_matches) ? verdict.diff_path_floor_matches : [];
        if (verdict.ok !== true) {
          diagnose("diff_floors", { ...verdict, error_code: DIFF_UNVERIFIABLE });
          return reply(false, `BLOCKED: ${DIFF_UNVERIFIABLE}: the diff of task ${taskId} in ${path} could not be checked against the project path floors; nothing was integrated.`, verdict);
        }
        if (verdict.blocked !== true) {
          // Recorded only when the diff hit a floor, so the trace shows which floors the real diff matched.
          if (matches.length > 0) await observe({ kind: "diff_floors", phase: approval.phase, status: "passed", matches });
          continue;
        }
        const required = (verdict.required ?? null) as Json | null;
        const toRoute = typeof required?.route === "string" ? required.route : null;
        if (!toRoute) {
          return reply(false, `BLOCKED: ${DIFF_UNDERRATED}: the diff of task ${taskId} needs a stronger route and none is available; nothing was integrated.`, verdict);
        }
        if (!await event({ kind: "diff_floors", phase: approval.phase, status: "underrated", from_route: approval.route,
                           to_route: toRoute, capacity_raised: verdict.capacity_raised === true, matches })) {
          return reply(false, "BLOCKED: parent provenance update failed; see diagnostic log.", parentFailure ?? {});
        }
        // The same state change as a recorded escalation: a new route epoch whose route_task must be rerun.
        pendingReroutes.add(taskId);
        routeEpochs.set(taskId, (routeEpochs.get(taskId) ?? 0) + 1);
        approvals.delete(taskId);
        for (const key of [...workspaces.keys()]) if (key.startsWith(`${taskId}\u0000`)) workspaces.delete(key);
        for (const [id, tasks] of [...implicitOpen]) {
          if (tasks.length === 1 && tasks[0] === taskId) { implicitOpen.delete(id); implicitEpoch.delete(id); }
        }
        const floored = [...new Set(matches.flatMap((match) => Array.isArray((match as Json).matched_paths)
          ? (match as Json).matched_paths as string[] : []))].sort().slice(0, 12);
        underrated.push(`${taskId} (ran ${approval.route}, needs ${toRoute}` +
          `${toRoute === approval.route ? " with a higher capacity" : ""}; floored paths ${floored.join(", ")})`);
        break;
      }
    }
    if (underrated.length === 0) return null;
    // Not "BLOCKED": the run is active and its next step is the re-route (#69).
    return reply(false, `NEXT STEP (${DIFF_UNDERRATED}): the run is active and not stopped. The diff of ${underrated.join("; ")} ` +
      "touches project path floors its classification did not cover, so the wave was not integrated. Add those paths to the " +
      "task's files_or_areas, rerun route_task and backend preflight, run the new route's steps in a fresh bound worktree, " +
      "then integrate again.", { error_code: DIFF_UNDERRATED, tasks: members.filter((id) => pendingReroutes.has(id)) });
  }

  async function integration(action: "integrate_begin" | "integrate_end", requestedWaveId: string) {
    // The lead names an implicitly recorded single-task wave as "auto".
    let waveId = requestedWaveId;
    if (requestedWaveId === "auto") {
      if (implicitOpen.size > 1) {
        return reply(false, `BLOCKED: WAVE_AUTO_AMBIGUOUS: ${implicitOpen.size} implicit waves are open (${[...implicitOpen.keys()].join(", ")}); name the one to integrate by its id.`);
      }
      if (implicitOpen.size === 0) {
        return reply(false, `BLOCKED: WAVE_UNKNOWN: "auto" names the one open implicit wave, and none is open${activeWave ? ` (the declared wave is ${activeWave.id})` : ""}.`);
      }
      waveId = [...implicitOpen.keys()][0];
    }
    const members = activeWave?.id === waveId ? [...activeWave.tasks.keys()] : implicitOpen.get(waveId);
    if (action === "integrate_begin") {
      if (!members) return reply(false, `BLOCKED: WAVE_UNKNOWN: ${waveId} is not an open wave.`);
      if (integrating) return reply(true, `Wave ${waveId} is already integrating.`);
      if (inFlight.size > 0) {
        return reply(false, `BLOCKED: INTEGRATION_WORKERS_IN_FLIGHT: ${inFlight.size} worker call(s) are still running; integrate only between waves.`);
      }
      const underrated = await diffFloorGate(members);
      if (underrated) return underrated;
      if (!await event({ kind: "wave", wave_id: waveId, task_ids: members, status: "integrating" })) {
        return reply(false, "BLOCKED: parent provenance update failed; see diagnostic log.", parentFailure ?? {});
      }
      integrating = waveId;
      return reply(true, `Integration of wave ${waveId} (task${members.length === 1 ? "" : "s"} ${members.join(", ")}) started. Worker launches are refused until integrate_end.`);
    }
    if (integrating !== waveId || !members) return reply(false, `BLOCKED: INTEGRATION_NOT_STARTED: wave ${waveId} is not integrating.`);
    // Re-base: the parent's own merges are the new reference for later worker snapshots.
    const rebased = await snapshot();
    if (!rebased) return reply(false, "BLOCKED: WORKER_SCOPE_UNVERIFIABLE: could not snapshot the parent checkout.");
    snapshots.clear();
    if (!await event({ kind: "wave", wave_id: waveId, task_ids: members, status: "integrated" })) {
      return reply(false, "BLOCKED: parent provenance update failed; see diagnostic log.", parentFailure ?? {});
    }
    integrating = null;
    implicitOpen.delete(waveId);
    implicitEpoch.delete(waveId);
    if (activeWave?.id === waveId) activeWave = null;
    return reply(true, `Wave ${waveId} integrated; the parent checkout snapshot is re-based. Plan the next dependency-ready wave.`,
      { wave_id: waveId, digest: rebased.digest });
  }

  pi.registerTool({
    name: "orchestration_record",
    label: "Record parent orchestration outcome",
    description: "Parent only: record phases, workers, child runs, mutation counts, concise numbered interaction summaries, or final status; bind mutating steps to prepared worktrees; declare waves; and bracket integration. Do not record directive or feedback interactions: the extension records each worker call itself, with task, step, route, a contract excerpt and digest, and a bounded result summary, digest and commit (the full text stays in the Pi session transcript). Record a decision interaction only for your own judgment, with task/step ids and optional reply_to. Never include prompts, raw outputs, transcripts, or issue bodies.",
    parameters: Type.Union([
      Type.Object({ action: Type.Literal("status") }, { additionalProperties: false }),
      Type.Object({ action: Type.Literal("bind_workspace"), task_id: Type.Optional(Type.String()),
        step_id: Type.String(), path: Type.String() }, { additionalProperties: false }),
      Type.Object({ action: Type.Literal("declare_wave"), wave_id: Type.String({ pattern: `^${TAG_ID}$` }),
        tasks: Type.Array(Type.Object({ task_id: Type.String({ pattern: `^${TAG_ID}$` }),
          write_scope: Type.Array(Type.String()) }, { additionalProperties: false }), { minItems: 1 }) },
        { additionalProperties: false }),
      Type.Object({ action: Type.Literal("integrate_begin"), wave_id: Type.String() }, { additionalProperties: false }),
      Type.Object({ action: Type.Literal("integrate_end"), wave_id: Type.String() }, { additionalProperties: false }),
      Type.Object({ action: Type.Literal("event"), event: EVENT }, { additionalProperties: false }),
      Type.Object({ action: Type.Literal("finish"),
        status: Type.Union([Type.Literal("COMPLETED"), Type.Literal("BLOCKED"),
                            Type.Literal("FAILED"), Type.Literal("PARTIAL")]),
        blocker_code: Type.Optional(Type.String()) }, { additionalProperties: false }),
    ]),
    async execute(_id, params) {
      if (!runId) {
        if (limitStopped) {
          return { content: [{ type: "text", text: limitStopped }],
                   details: { ok: false, active: false, run_id: lastRunId, result: { error_code: "LEAD_CALL_LIMIT" } } };
        }
        if (params.action === "status" && lastRunId && lastTerminal && !parentFailure) {
          return { content: [{ type: "text", text: `Parent orchestration run ${lastRunId} finalized (ok: false, active: false).` }],
                   details: { ok: false, active: false, run_id: lastRunId, result: lastTerminal } };
        }
        const result = parentFailure ?? { ok: false, error_category: "NoActiveParentRun" };
        diagnose(params.action, result);
        const code = typeof result.error_code === "string" && /^[A-Z][A-Z0-9_]{0,99}$/.test(result.error_code)
          ? ` (${result.error_code})` : "";
        return { content: [{ type: "text", text: `BLOCKED${code}: no active parent orchestration record. Start with /orchestrate <objective> in this Pi session, or resume the saved session; recorder writes outside that run are prohibited. See diagnostic log.` }],
                 details: { ok: false, active: false, run_id: null as string | null, result } };
      }
      if (params.action === "status" && pendingReroutes.size > 0 && !parentBlocked) {
        // Not "BLOCKED": a resumed lead is told to stop on BLOCKED, and this is its expected next step (#69).
        return { content: [{ type: "text", text: `NEXT STEP (PARENT_ROUTE_RECHECK_REQUIRED): the run is active and not stopped. Rerun route_task and complete backend preflight for ${[...pendingReroutes].sort().join(", ")}, then call status again before dispatching workers.` }],
          details: { ok: false, active: true, run_id: runId,
            result: { error_code: "PARENT_ROUTE_RECHECK_REQUIRED" } } };
      }
      if (params.action === "event" && params.event.kind === "escalation") {
        const phase = params.event.phase;
        if ([...inFlight.values()].some((worker) => worker.taskId === phase)) {
          return reply(false, "BLOCKED: PARENT_ESCALATION_WORKERS_IN_FLIGHT: wait for this task's workers to finish before escalating.");
        }
      }
      if (params.action === "bind_workspace") {
        const taskId = params.task_id ?? (approvals.size === 1 ? [...approvals.keys()][0] : null);
        if (!taskId) {
          return reply(false, "BLOCKED: WORKER_TASK_UNATTRIBUTED: pass task_id; several (or no) tasks are routed.");
        }
        const checked = targetCwd
          ? await command(SESSION_SCOPE, [], { operation: "worktree", target_cwd: targetCwd, path: params.path })
          : { ok: false };
        const ok = checked.ok === true && typeof checked.path === "string";
        if (ok) {
          workspaces.set(workspaceKey(taskId, params.step_id), checked.path as string);
          await event({ kind: "checkpoint", phase: approvals.get(taskId)?.phase ?? taskId, reason: "worker_approval",
            workspace_bound: true });
        }
        return reply(ok, ok
          ? `Step ${params.step_id} of task ${taskId} is bound to worktree ${String(checked.path)}.`
          : checked.blocker_code === "WORKER_WORKSPACE_PROTECTED_PATH"
            ? "BLOCKED: WORKER_WORKSPACE_PROTECTED_PATH: the worktree is inside a .git directory, where Claude Code denies every edit; create it outside .git (for example .pi/worktrees/<task>) and bind that path."
            : "BLOCKED: WORKER_WORKSPACE_INVALID: the path must be a linked worktree of the target repository, not the parent checkout.",
          checked);
      }
      if (params.action === "declare_wave") return declareWave(params.wave_id, params.tasks);
      if (params.action === "integrate_begin" || params.action === "integrate_end") {
        return integration(params.action, params.wave_id);
      }
      const args = params.action === "status" ? ["status", runId]
        : params.action === "event" ? ["event", runId, ...ownerArgs()]
        : ["finish", runId, params.status, ...ownerArgs(),
           ...(params.blocker_code ? ["--blocker-code", params.blocker_code] : [])];
      const result = await command(PARENT, args,
        // `only_if_open` and `auto_recorded` mark the extension's own records: a lead payload never carries them.
        params.action === "event" ? withoutAutomaticMarks(params.event as Json) : undefined);
      const observedRunId = runId;
      const unresolvedRoute = parentBlocked &&
        (parentFailure?.error_code === "PARENT_EVENT_CONFLICT" || parentFailure?.error_code === "PARENT_ESCALATION_INVALID"
         || parentFailure?.error_code === "PARENT_ESCALATION_WORKERS_IN_FLIGHT");
      const ok = result.ok !== false && (params.action !== "status" || result.status === "IN_PROGRESS")
        && !(params.action === "status" && unresolvedRoute);
      // A refused repeat or excess override of an already-terminal worker changes nothing: it is reported to the
      // lead and never blocks the run.
      const restated = params.action === "event" && params.event.kind === "worker"
        && autoWorkers.has(workspaceKey(params.event.phase, params.event.step_id));
      const harmless = params.action === "event" && result.ok === false
        && (HARMLESS_REFUSALS.has(String(result.error_code)) || (restated && RESTATEMENT_REFUSALS.has(String(result.error_code))));
      if (!ok && !harmless) {
        const failure = params.action === "status" && parentBlocked ? parentFailure ?? result : result;
        diagnose(params.action, failure);
        parentFailure = failure; parentBlocked = true; blockAllWorktrees();
      }
      if (params.action === "event" && params.event.kind === "escalation" && ok) {
        parentFailure = null; parentBlocked = false;
        pendingReroutes.add(params.event.phase);
        routeEpochs.set(params.event.phase, (routeEpochs.get(params.event.phase) ?? 0) + 1);
        approvals.delete(params.event.phase);
        for (const key of workspaces.keys()) {
          if (key.startsWith(`${params.event.phase}\u0000`)) workspaces.delete(key);
        }
      }
      if (params.action === "status" && ok) { parentFailure = null; parentBlocked = false; }
      if (params.action === "finish" && ok) {
        lastTerminal = result;
        persist(false);
        runId = null;
        stopHeartbeat();
        blockAllWorktrees();
      }
      const effectiveResult = ok ? result : parentFailure ?? result;
      const code = typeof effectiveResult.blocker_code === "string" ? effectiveResult.blocker_code
        : typeof effectiveResult.error_code === "string" ? effectiveResult.error_code : null;
      // The model reads only this text, never `details`, so the status line states ok/active explicitly.
      const message = harmless ? `Worker override refused (${code}); nothing changed and the run is not blocked. Record a different judgment or leave the worker as recorded.`
        : ok ? (params.action === "status"
          ? `Parent orchestration record ${observedRunId} is ready (ok: true, active: ${runId !== null}).` +
            (leadLimit && runId ? ` Lead calls: ${leadLimit.calls} of ${leadLimit.max}${leadLimit.reached ? " (limit reached: finish now)" : ""}.` : "")
        : params.action === "finish" ? completionSummary(result, observedRunId)
        : "Parent orchestration trace updated.")
        : `BLOCKED: parent orchestration ${params.action} failed${code ? ` (${code})` : ""}` +
          (code === "PARENT_EVENT_CONFLICT" ? ". Record an explicit valid escalation event, then rerun route_task" : "") +
          // Completion refusals carry only phase/step ids and fixed wording, so the model may read them.
          ((code === "PARENT_COMPLETION_INCOMPLETE" || code === "PARENT_REVIEW_MISSING") && typeof result.error === "string"
            ? `: ${result.error.slice(0, 600)}` : "; see diagnostic log.");
      return { content: [{ type: "text", text: message }],
               details: { ok, active: runId !== null, run_id: observedRunId, result } };
    },
  });

  pi.registerTool({
    name: "ClaudeWorker",
    label: "Claude Code worker",
    description: "Run a routed Claude step as a headless Claude Code subprocess. Its working directory is the step's bound worktree (a read-only step without one runs read-only tools in the target checkout); model and thinking come from the routed capacity assignment. Start the prompt with [pico-task <task_id>:<step_id>] and give the complete worker contract. Several ClaudeWorker calls in one tool batch run concurrently.",
    promptSnippet: "Run a routed Claude step headless in its bound worktree.",
    parameters: Type.Object({
      prompt: Type.String({ description: "Complete worker contract, starting with [pico-task <task_id>:<step_id>]." }),
      model: Type.Optional(Type.String({ description: "Routed canonical model; omit to use the route's." })),
      thinking: Type.Optional(EFFORT),
    }, { additionalProperties: false }),
    executionMode: "parallel",
    async execute(toolCallId, params, signal) {
      const plan = workerPlans.get(toolCallId);
      workerPlans.delete(toolCallId);
      if (!plan) {
        return { content: [{ type: "text" as const, text: "BLOCKED: WORKER_TASK_UNROUTED: ClaudeWorker runs only for a routed Claude step the orchestration lifecycle resolved." }],
                 details: { ok: false } as Json, isError: true };
      }
      // Aborted by the tool signal, or by this lifecycle when the run lease is lost.
      const controller = new AbortController();
      workerAborts.set(toolCallId, controller);
      let outcome: ClaudeWorkerOutcome;
      try {
        outcome = await runClaudeWorker(plan, params.prompt,
          signal ? AbortSignal.any([signal, controller.signal]) : controller.signal);
      } finally {
        workerAborts.delete(toolCallId);
      }
      const notes = [outcome.text];
      if (outcome.permissionDenials > 0) {
        const denied = outcome.deniedCalls.length ? `: ${outcome.deniedCalls.join("; ")}` : ".";
        notes.push(`Claude Code denied ${outcome.permissionDenials} tool call(s) outside the worker's permissions${denied}`);
      }
      if (!outcome.ok) {
        notes.push(`Orchestration blocker: ${workerBlocker(outcome)}. Canonical model: ${plan.canonical}; Claude Code model: ${plan.backendModel}. Do not infer entitlement from an ambiguous backend error.`);
      }
      return { content: [{ type: "text" as const, text: notes.filter(Boolean).join("\n\n") }], isError: !outcome.ok,
        details: { ok: outcome.ok, task_id: plan.taskId, step_id: plan.stepId, mode: plan.mode, cwd: plan.cwd,
          model: plan.canonical, backend: "claude-code", backend_model: plan.backendModel,
          reasoning_effort: plan.effort, exit_code: outcome.exitCode, error: outcome.error,
          blocker_code: outcome.ok ? null : workerBlocker(outcome), usage: outcome.usage,
          duration_ms: outcome.durationMs, elapsed_ms: outcome.elapsedMs, session_id: outcome.sessionId,
          permission_denials: outcome.permissionDenials, denied_calls: outcome.deniedCalls,
          // Names only, never values, of parent environment variables withheld from the worker.
          env_dropped_count: outcome.envDropped.length, env_dropped: outcome.envDropped.slice(0, 100) } as Json };
    },
  });

  pi.on("turn_end", async (_event, ctx) => {
    lastCtx = ctx;
    // Worker tools block within one turn: anything still marked in flight never reported a result.
    for (const id of [...inFlight.keys()]) { snapshots.delete(id); await workerFinished(id); }
    workerPlans.clear();
    if (runId) await renewLease();
    if (!runId || parentBlocked || !budget) return;
    const tokens = ctx.getContextUsage()?.tokens;
    if (typeof tokens !== "number") return;
    const firstObservation = budget.baseline === null;
    const observed = observeContext(budget, tokens);
    budget = observed.state;
    if (firstObservation) {
      await observe({ kind: "context", baseline_tokens: tokens, context_tokens: tokens, working_tokens: 0 });
    } else if (observed.working >= recordedPeak + 1000) {
      recordedPeak = observed.working;
      await observe({ kind: "context", context_tokens: tokens, working_tokens: observed.working });
    }
  });

  pi.on("turn_start", (event) => { if (runId) currentTurn = event.turnIndex; });
  pi.on("message_start", (event) => {
    if (runId && event.message.role === "assistant") messageStarts.set(event.message.timestamp, Date.now());
  });

  pi.on("message_end", async (event) => {
    if (!runId || event.message.role !== "assistant") return;
    const message = event.message;
    const model = `${message.provider}/${message.model}`;
    // The limit counts every assistant call, including one whose model string fails validation.
    if (leadLimit) leadLimit.calls += 1;
    if (/^[A-Za-z0-9_.:/-]{1,128}$/.test(model)) {
      const started = messageStarts.get(message.timestamp);
      messageStarts.delete(message.timestamp);
      const approval = approvals.size === 1 ? approvals.get(lastPhase) : undefined;
      const leadCost = (message.usage as { cost?: { total?: unknown } } | undefined)?.cost?.total; // Pi's per-message estimate
      await observe({ kind: "lead_usage", model, phase: lastPhase,
        task_type: approval?.taskType ?? null, turn_index: currentTurn,
        duration_ms: started === undefined ? null : Math.max(0, Date.now() - started),
        input_tokens: count(message.usage?.input), output_tokens: count(message.usage?.output),
        cached_tokens: count(message.usage?.cacheRead), total_tokens: count(message.usage?.totalTokens),
        ...(count(message.usage?.cacheWrite) === null ? {} : { cache_write_tokens: count(message.usage?.cacheWrite) }),
        ...(typeof leadCost === "number" && Number.isFinite(leadCost) && leadCost >= 0 ? { estimated_cost_usd: leadCost } : {}) });
    }
    if (!leadLimit || !runId) return;
    await crossThresholds();
    // The call that uses the last grace slot may still carry the finish; any call beyond it ends the run.
    const finishing = Array.isArray(message.content) && message.content.some((part: any) => part?.type === "toolCall"
      && part.name === "orchestration_record" && part.arguments?.action === "finish");
    const last = leadLimit.max + LEAD_CALL_GRACE;
    if (leadLimit.calls > last || (leadLimit.calls === last && !finishing)) await stopAtLeadLimit();
  });

  pi.on("session_compact", async (event) => {
    compactionFailed = false;
    if (!runId || parentBlocked) return;
    // Observe Pi's compaction without changing its summary, cut point, or run state.
    await observe({ kind: "context", compaction: { reason: event.reason, structured: false,
      tokens_before: count(event.compactionEntry?.tokensBefore) } });
  });

  pi.on("session_compact_failed", async () => { compactionFailed = true; });

  pi.on("session_shutdown", async (event) => {
    if (event.reason === "quit" || event.reason === "new" || event.reason === "fork") await settle();
    // Reload and explicit resume preserve the durable identity in this session;
    // the restoring instance renews the lease under the same owner id.
    stopHeartbeat();
  });
  pi.on("agent_before_settle", async (event, ctx) => {
    if (event.outcome === "aborted" && !event.continue && !compactionFailed &&
        !ctx.hasPendingMessages?.()) await settle();
    compactionFailed = false;
  });
}
}

export default createOrchestrationLifecycle();
