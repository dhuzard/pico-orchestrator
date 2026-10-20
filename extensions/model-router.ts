/**
 * model-router: registers the `route_task` tool.
 *
 * This extension is a thin adapter: it contains no route predicates,
 * precedence logic, risk-floor logic, or manually reproduced task schema
 * fields. Every routing decision is delegated to the shared Python routing
 * engine (tools/routing_engine.py) via the stdio adapter tools/route_task.py.
 * The tool's parameter schema is loaded directly from
 * schemas/task.schema.json (via Type.Unsafe) so the field list is never
 * duplicated here.
 *
 * Requires this repository's full tree (tools/, schemas/, routing/) to be
 * present alongside this file -- copying this .ts file in isolation is not
 * supported, since it spawns tools/route_task.py and loads
 * schemas/task.schema.json from paths resolved relative to itself.
 */

import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { CONFIG_DIR_NAME } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { Value } from "typebox/value";

const EXTENSION_DIR = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(EXTENSION_DIR, "..");
const ROUTE_TASK_SCRIPT = join(REPO_ROOT, "tools", "route_task.py");
const VALIDATE_ROUTE_RESULT_SCRIPT = join(REPO_ROOT, "tools", "validate_route_result.py");
const BACKEND_PREFLIGHT_SCRIPT = join(REPO_ROOT, "tools", "backend_preflight.py");
const TASK_SCHEMA_PATH = join(REPO_ROOT, "schemas", "task.schema.json");
const EXECUTION_PLAN_SCHEMA_PATH = join(REPO_ROOT, "schemas", "execution-plan.schema.json");
const PROJECT_ROUTING_FILE = "routing.yaml";

function pythonExecutable(): string {
	return process.env.PI_ROUTER_PYTHON ?? "python3";
}

/** Bounds tool text/error previews so a misbehaving adapter can never flood context. */
const MAX_TEXT_BYTES = 512 * 1024;

interface RouteTaskEnvelope {
	ok: boolean;
	[key: string]: unknown;
}

interface ObservationalTelemetry {
	schema_version: 1;
	source: string;
	started_at: string;
	ended_at: string;
	duration_ms: number;
	queue_ms: null;
	input_tokens: null;
	output_tokens: null;
	cached_tokens: null;
	total_tokens: null;
	estimated_cost_usd: null;
	input_bytes: null;
	output_bytes: null;
}

interface RouteTaskDetails {
	ok: boolean;
	result?: RouteTaskEnvelope;
	error?: string;
	blocker_code?: string;
	preflight?: PreflightResult;
	availability_blockers?: AvailabilityBlocker[];
	routing_telemetry?: ObservationalTelemetry;
}

interface PreflightResult {
	ok?: boolean;
	blocker_code?: string;
	remediation?: string;
	[key: string]: unknown;
}

interface AvailabilityBlocker {
	route: string;
	blocker_code: string;
}

/** Mirrors tools/backend_preflight.py REROUTABLE_BLOCKERS: candidate-availability only. */
const REROUTABLE_BLOCKERS: ReadonlySet<string> = new Set([
	"MODEL_BACKEND_UNAVAILABLE",
	"MODEL_AUTH_UNAVAILABLE",
	"MODEL_BACKEND_VERSION_UNSUPPORTED",
]);
/** One exclusion per policy route (4) at most, then a final selection attempt. */
const MAX_ROUTE_EXCLUSIONS = 4;

function preflightBlocked(
	preflight: PreflightResult,
	availabilityBlockers: AvailabilityBlocker[],
	routingTelemetry: ObservationalTelemetry,
): { content: { type: "text"; text: string }[]; details: RouteTaskDetails } {
	const code = String(preflight.blocker_code);
	const remediation = typeof preflight.remediation === "string" ? ` ${preflight.remediation}` : "";
	const message =
		`route_task backend preflight BLOCKED (${code}); failing closed without rerouting.${remediation}`;
	return {
		content: [{ type: "text", text: message }],
		details: {
			ok: false,
			error: message,
			blocker_code: code,
			preflight,
			availability_blockers: availabilityBlockers,
			routing_telemetry: routingTelemetry,
		},
	};
}

function startRouteTaskTelemetry() {
	return { startedAt: new Date().toISOString(), monotonicStart: performance.now() };
}

function finishRouteTaskTelemetry(start: ReturnType<typeof startRouteTaskTelemetry>): ObservationalTelemetry {
	return {
		schema_version: 1,
		source: "route_task",
		started_at: start.startedAt,
		ended_at: new Date().toISOString(),
		duration_ms: Math.max(0, performance.now() - start.monotonicStart),
		queue_ms: null,
		input_tokens: null,
		output_tokens: null,
		cached_tokens: null,
		total_tokens: null,
		estimated_cost_usd: null,
		input_bytes: null,
		output_bytes: null,
	};
}

function compactContentEnvelope(parsed: RouteTaskEnvelope): RouteTaskEnvelope {
	if (parsed.ok === true) {
		return {
			ok: true,
			policy_version: parsed.policy_version,
			selected_route: parsed.selected_route,
			execution_plan: parsed.execution_plan,
			capacity_assignments: parsed.capacity_assignments,
			backend_bindings: parsed.backend_bindings,
			fallback_applied: parsed.fallback_applied,
			matches: parsed.matches,
			route_availability: parsed.route_availability,
			content_truncated: true,
			content_note:
				"Bulky fields were omitted from tool content to keep context bounded; full structured result is available in tool details.",
		};
	}

	const errors = Array.isArray(parsed.errors) ? parsed.errors.slice(0, 5) : [];
	return {
		ok: false,
		...(typeof parsed.blocker_code === "string" ? { blocker_code: parsed.blocker_code } : {}),
		...(Array.isArray(parsed.missing_evidence_fields) ? { missing_evidence_fields: parsed.missing_evidence_fields } : {}),
		error_count: Array.isArray(parsed.errors) ? parsed.errors.length : undefined,
		errors,
		content_truncated: true,
		content_note:
			"Validation details were abbreviated in tool content to keep context bounded; full structured result is available in tool details.",
	};
}

function jsonForContent(parsed: RouteTaskEnvelope): string {
	const fullText = JSON.stringify(parsed);
	if (Buffer.byteLength(fullText, "utf-8") <= MAX_TEXT_BYTES) return fullText;
	const compactText = JSON.stringify(compactContentEnvelope(parsed));
	if (Buffer.byteLength(compactText, "utf-8") <= MAX_TEXT_BYTES) return compactText;
	return JSON.stringify({
		ok: parsed.ok,
		content_truncated: true,
		content_note: "route_task result exceeded the content budget; full structured result is available in tool details.",
	});
}

function readTaskSchema(): Record<string, unknown> {
	const raw = readFileSync(TASK_SCHEMA_PATH, "utf-8");
	return JSON.parse(raw) as Record<string, unknown>;
}

function readExecutionPlanSchema(): Record<string, unknown> {
	const raw = readFileSync(EXECUTION_PLAN_SCHEMA_PATH, "utf-8");
	return JSON.parse(raw) as Record<string, unknown>;
}

/**
 * Structural (JSON Schema shape) check only -- this mirrors
 * schemas/execution-plan.schema.json via TypeBox's `Value.Check` against a
 * `Type.Unsafe`-wrapped raw schema, exactly like the tool's own parameter
 * schema above. It intentionally does NOT re-implement the semantic rules
 * (role/intent/mutation/dependency/independent-review invariants): those
 * live solely in tools/routing_engine.py, the single plan interpreter. This
 * is a boundary check against a missing or structurally corrupt plan
 * crossing the subprocess boundary as if it were trustworthy, not a second
 * routing/execution-plan interpreter.
 */
function isStructurallyValidExecutionPlan(executionPlanSchema: Record<string, unknown>, plan: unknown): boolean {
	const ExecutionPlan = Type.Unsafe<Record<string, unknown>>(executionPlanSchema);
	return Value.Check(ExecutionPlan, plan);
}

interface SpawnCaptureResult {
	code: number | null;
	signalName: NodeJS.Signals | null;
	stdout: string;
	stderr: string;
	stderrTruncated: boolean;
	spawnError?: Error;
}

const ADAPTER_TIMEOUT_MS = 60_000;
const MAX_PROTOCOL_BYTES = 8 * 1024 * 1024;

export function runJsonAdapter(
	scriptPath: string,
	args: string[],
	payload: unknown,
	signal: AbortSignal | undefined,
	timeoutMs = ADAPTER_TIMEOUT_MS,
): Promise<SpawnCaptureResult> {
	return new Promise((resolve) => {
		const child = spawn(
			pythonExecutable(),
			[scriptPath, ...args],
			{
				cwd: REPO_ROOT,
				stdio: ["pipe", "pipe", "pipe"],
				signal,
			},
		);

		let stderrBytes = 0;
		let stderrTruncated = false;
		let stdoutBytes = 0;
		const stdoutChunks: Buffer[] = [];
		const stderrChunks: Buffer[] = [];

		// The authoritative result includes the validated classification, so
		// allow a generous protocol budget while bounding a flooding adapter.
		child.stdout?.on("data", (chunk: Buffer) => {
			stdoutBytes += chunk.length;
			if (stdoutBytes > MAX_PROTOCOL_BYTES) {
				child.kill("SIGKILL");
				finish({ code: null, signalName: "SIGKILL", stdout: "", stderr: "",
					stderrTruncated: false, spawnError: new Error("adapter output exceeded 8 MiB") });
				return;
			}
			stdoutChunks.push(chunk);
		});
		child.stderr?.on("data", (chunk: Buffer) => {
			const remaining = MAX_TEXT_BYTES - stderrBytes;
			if (remaining > 0) {
				const kept = chunk.length > remaining ? chunk.subarray(0, remaining) : chunk;
				stderrChunks.push(kept);
				stderrBytes += kept.length;
			}
			if (chunk.length > remaining) {
				stderrTruncated = true;
			}
		});

		let settled = false;
		const finish = (result: SpawnCaptureResult) => {
			if (settled) return;
			settled = true;
			clearTimeout(timer);
			resolve(result);
		};
		const timer = setTimeout(() => {
			child.kill("SIGKILL");
			finish({ code: null, signalName: "SIGKILL", stdout: "", stderr: "",
				stderrTruncated: false, spawnError: new Error("adapter timed out") });
		}, timeoutMs);

		child.on("error", (err: Error) => {
			finish({
				code: null,
				signalName: null,
				stdout: Buffer.concat(stdoutChunks).toString("utf-8"),
				stderr: Buffer.concat(stderrChunks).toString("utf-8"),
				stderrTruncated,
				spawnError: err,
			});
		});

		child.on("close", (code, signalName) => {
			finish({
				code,
				signalName,
				stdout: Buffer.concat(stdoutChunks).toString("utf-8"),
				stderr: Buffer.concat(stderrChunks).toString("utf-8"),
				stderrTruncated,
			});
		});

		child.stdin?.on("error", () => {
			// A closed/broken stdin pipe is reported via 'error' or 'close' above.
		});

		try {
			child.stdin?.write(JSON.stringify(payload));
			child.stdin?.end();
		} catch {
			// Write failures surface through the 'error'/'close' handlers.
		}
	});
}

function runRouteTaskAdapter(
	classifiedTask: unknown,
	projectRoutingPath: string,
	signal: AbortSignal | undefined,
	unavailableRoutes: string[] = [],
): Promise<SpawnCaptureResult> {
	return runJsonAdapter(
		ROUTE_TASK_SCRIPT,
		["--project-routing", projectRoutingPath],
		{ classified_task: classifiedTask, unavailable_routes: unavailableRoutes },
		signal,
	);
}

function runBackendPreflight(
	routeTaskResult: unknown,
	availableTools: string[],
	cwd: string,
	signal: AbortSignal | undefined,
): Promise<SpawnCaptureResult> {
	return runJsonAdapter(
		BACKEND_PREFLIGHT_SCRIPT,
		[],
		{ route_task_result: routeTaskResult, available_tools: availableTools, cwd },
		signal,
	);
}

function runRouteResultValidator(result: unknown, signal: AbortSignal | undefined): Promise<SpawnCaptureResult> {
	return runJsonAdapter(VALIDATE_ROUTE_RESULT_SCRIPT, [], { result }, signal);
}

export default function modelRouterExtension(pi: ExtensionAPI) {
	const taskSchema = readTaskSchema();
	const executionPlanSchema = readExecutionPlanSchema();

	pi.registerTool({
		name: "route_task",
		label: "Route task",
		description:
			"Deterministically select the execution route (CODEX, CLAUDE, CLAUDE_CODEX, or " +
			"CODEX_CLAUDE_CODEX) for a fully classified engineering task, per routing/default-policy.yaml. " +
			"The LLM classifies the task; this tool -- not the LLM -- selects the route by delegating to " +
			"the shared deterministic routing engine. Also applies any project-local risk_overrides floors " +
			"from .pi/routing.yaml. On an invalid task or project config, returns ok:false with structured " +
			"validation errors and no route: repair the classification or config and retry, never pick a " +
			"route without calling this tool again. Escalating values (failure_cost=high, semantic_correctness=critical, " +
			"ambiguity=high, behavior_preservation=required, assumptions_must_be_explicit, security_sensitive, " +
			"destructive_operation) each need an escalation_evidence entry, otherwise the result is ok:false with " +
			"blocker_code CLASSIFICATION_EVIDENCE_MISSING. Successful results include policy_version plus a v3 authored " +
			"execution plan and post-route capacity_assignments with capacity, reasoning-effort, and context-strategy metadata.",
		parameters: Type.Object({
			classified_task: Type.Unsafe<Record<string, unknown>>(taskSchema),
		}),
		async execute(_toolCallId, params, signal, _onUpdate, ctx) {
			const projectRoutingPath = join(ctx.cwd, CONFIG_DIR_NAME, PROJECT_ROUTING_FILE);

			if (signal?.aborted) {
				return {
					content: [{ type: "text", text: "route_task cancelled before execution." }],
					details: { ok: false, error: "cancelled" } satisfies RouteTaskDetails,
				};
			}

			const telemetryStart = startRouteTaskTelemetry();
			const routingTelemetry = () => finishRouteTaskTelemetry(telemetryStart);
			const availableTools = typeof (pi as unknown as { getActiveTools?: () => string[] }).getActiveTools === "function"
				? (pi as unknown as { getActiveTools: () => string[] }).getActiveTools()
				: ["route_task", "spawn_subagent", "ClaudeWorker"];
			const availabilityPreflightEnabled = availableTools.includes("orchestration_record");
			const unavailableRoutes: string[] = [];
			const availabilityBlockers: AvailabilityBlocker[] = [];
			let parsed: RouteTaskEnvelope | undefined;
			let resolved = false;

			for (let attempt = 0; attempt <= MAX_ROUTE_EXCLUSIONS; attempt += 1) {
				const capture = await runRouteTaskAdapter(params.classified_task, projectRoutingPath, signal, unavailableRoutes);

				if (signal?.aborted) {
					return {
						content: [{ type: "text", text: "route_task cancelled." }],
						details: { ok: false, error: "cancelled", routing_telemetry: routingTelemetry() } satisfies RouteTaskDetails,
					};
				}

				if (capture.spawnError) {
					const message = `route_task adapter failed to start: ${capture.spawnError.message}`;
					return {
						content: [{ type: "text", text: message }],
						details: { ok: false, error: message, routing_telemetry: routingTelemetry() } satisfies RouteTaskDetails,
					};
				}

				if (capture.code !== 0) {
					const message =
						`route_task adapter exited with code ${capture.code}${capture.signalName ? ` (signal ${capture.signalName})` : ""}.\n` +
						`stderr${capture.stderrTruncated ? ` (truncated to ${MAX_TEXT_BYTES} bytes)` : ""}:\n${capture.stderr.trim() || "(empty)"}`;
					return {
						content: [{ type: "text", text: message }],
						details: { ok: false, error: message, routing_telemetry: routingTelemetry() } satisfies RouteTaskDetails,
					};
				}

				try {
					parsed = JSON.parse(capture.stdout) as RouteTaskEnvelope;
				} catch (err) {
					const stdoutPreview = capture.stdout.length > MAX_TEXT_BYTES
						? `${capture.stdout.slice(0, MAX_TEXT_BYTES)}\n[stdout truncated to ${MAX_TEXT_BYTES} bytes]`
						: capture.stdout;
					const message =
						`route_task adapter produced malformed output: ${(err as Error).message}\n` +
						`stdout:\n${stdoutPreview.trim() || "(empty)"}\n` +
						`stderr${capture.stderrTruncated ? ` (truncated to ${MAX_TEXT_BYTES} bytes)` : ""}:\n${capture.stderr.trim() || "(empty)"}`;
					return {
						content: [{ type: "text", text: message }],
						details: { ok: false, error: message, routing_telemetry: routingTelemetry() } satisfies RouteTaskDetails,
					};
				}

				if (parsed.ok === true) {
					if (!isStructurallyValidExecutionPlan(executionPlanSchema, parsed.execution_plan)) {
						const message =
							"route_task adapter returned ok:true with a missing or schema-invalid execution_plan " +
							"(schemas/execution-plan.schema.json); refusing to return an unvalidated route.";
						return {
							content: [{ type: "text", text: message }],
							details: { ok: false, error: message, routing_telemetry: routingTelemetry() } satisfies RouteTaskDetails,
						};
					}

					const validation = await runRouteResultValidator(parsed, signal);
					if (signal?.aborted) {
						return {
							content: [{ type: "text", text: "route_task result validation cancelled." }],
							details: { ok: false, error: "cancelled", routing_telemetry: routingTelemetry() } satisfies RouteTaskDetails,
						};
					}
					if (validation.spawnError || validation.code !== 0) {
						const message = validation.spawnError
							? `route_task result validator failed to start: ${validation.spawnError.message}`
							: `route_task result validator exited with code ${validation.code}${validation.signalName ? ` (signal ${validation.signalName})` : ""}.\n` +
								`stderr${validation.stderrTruncated ? ` (truncated to ${MAX_TEXT_BYTES} bytes)` : ""}:\n${validation.stderr.trim() || "(empty)"}`;
						return {
							content: [{ type: "text", text: message }],
							details: { ok: false, error: message, routing_telemetry: routingTelemetry() } satisfies RouteTaskDetails,
						};
					}
					let validatorResult: { ok?: boolean; errors?: unknown[] };
					try {
						validatorResult = JSON.parse(validation.stdout) as { ok?: boolean; errors?: unknown[] };
					} catch (err) {
						const message = `route_task result validator produced malformed output: ${(err as Error).message}`;
						return {
							content: [{ type: "text", text: message }],
							details: { ok: false, error: message, routing_telemetry: routingTelemetry() } satisfies RouteTaskDetails,
						};
					}
					if (validatorResult.ok !== true) {
						const message =
							"route_task adapter returned ok:true with a semantically invalid execution result; " +
							`refusing to return an unvalidated route: ${JSON.stringify(validatorResult.errors ?? [])}`;
						return {
							content: [{ type: "text", text: message }],
							details: { ok: false, error: message, routing_telemetry: routingTelemetry() } satisfies RouteTaskDetails,
						};
					}

					if (!availabilityPreflightEnabled) {
						resolved = true;
						break;
					}
					const preflight = await runBackendPreflight(parsed, availableTools, ctx.cwd, signal);
					if (signal?.aborted) {
						return {
							content: [{ type: "text", text: "route_task backend preflight cancelled." }],
							details: { ok: false, error: "cancelled", routing_telemetry: routingTelemetry() } satisfies RouteTaskDetails,
						};
					}
					let preflightResult: PreflightResult | undefined;
					if (!preflight.spawnError) {
						try {
							const value = JSON.parse(preflight.stdout) as unknown;
							if (value && typeof value === "object" && !Array.isArray(value)) preflightResult = value as PreflightResult;
						} catch {
							preflightResult = undefined;
						}
					}
					if (preflightResult?.ok === true && preflight.code === 0) {
						resolved = true;
						break;
					}
					if (preflight.spawnError || !preflightResult || typeof preflightResult.blocker_code !== "string") {
						const message = preflight.spawnError
							? `route_task backend preflight failed to start: ${preflight.spawnError.message}`
							: `route_task backend preflight produced no structured result (exit code ${preflight.code}${preflight.signalName ? `, signal ${preflight.signalName}` : ""}); failing closed.\n` +
								`stderr${preflight.stderrTruncated ? ` (truncated to ${MAX_TEXT_BYTES} bytes)` : ""}:\n${preflight.stderr.trim() || "(empty)"}`;
						return {
							content: [{ type: "text", text: message }],
							details: { ok: false, error: message, routing_telemetry: routingTelemetry() } satisfies RouteTaskDetails,
						};
					}
					const selectedRoute = String(parsed.selected_route ?? "");
					availabilityBlockers.push({ route: selectedRoute, blocker_code: preflightResult.blocker_code });
					// Only "this candidate cannot run here" blockers may exclude a
					// route and re-select. Identity mismatch, an uninitialized launcher
					// pin, invalid routes, or invalid configuration stop the run.
					if (REROUTABLE_BLOCKERS.has(preflightResult.blocker_code) && selectedRoute && !unavailableRoutes.includes(selectedRoute)) {
						unavailableRoutes.push(selectedRoute);
						continue;
					}
					return preflightBlocked(preflightResult, availabilityBlockers, routingTelemetry());
				}
				// ok:false (validation errors or NO_AVAILABLE_ROUTE) is returned as-is.
				resolved = true;
				break;
			}

			if (parsed === undefined || !resolved) {
				const message = "route_task exhausted availability attempts without a preflighted route; failing closed.";
				return {
					content: [{ type: "text", text: message }],
					details: { ok: false, error: message, availability_blockers: availabilityBlockers, routing_telemetry: routingTelemetry() } satisfies RouteTaskDetails,
				};
			}

			if (parsed.ok !== true && parsed.blocker_code === "NO_AVAILABLE_ROUTE") {
				const last = availabilityBlockers.at(-1)?.blocker_code ?? "UNKNOWN";
				const message =
					`route_task found no available policy-valid route (NO_AVAILABLE_ROUTE); excluded ${JSON.stringify(unavailableRoutes)}, ` +
					`last backend blocker: ${last}. Do not choose a route manually; restore the backend or escalate.`;
				return {
					content: [{ type: "text", text: message }],
					details: { ok: false, result: parsed, error: message, availability_blockers: availabilityBlockers, routing_telemetry: routingTelemetry() } satisfies RouteTaskDetails,
				};
			}

			if (parsed.ok !== true && parsed.blocker_code === "CLASSIFICATION_EVIDENCE_MISSING") {
				const fields = Array.isArray(parsed.missing_evidence_fields) ? parsed.missing_evidence_fields : [];
				const message =
					`route_task rejected the classification (CLASSIFICATION_EVIDENCE_MISSING): ${JSON.stringify(fields)} ` +
					"use escalating values without escalation_evidence. For each field either add a one-line, task-specific " +
					"escalation_evidence entry or lower it to its low-risk value, then call route_task again.";
				return {
					content: [{ type: "text", text: message }],
					details: {
						ok: false,
						result: parsed,
						error: message,
						blocker_code: "CLASSIFICATION_EVIDENCE_MISSING",
						routing_telemetry: routingTelemetry(),
					} satisfies RouteTaskDetails,
				};
			}

			const text = jsonForContent(parsed);

			return {
				content: [{ type: "text", text }],
				details: {
					ok: parsed.ok,
					result: parsed,
					...(availabilityBlockers.length > 0 ? { availability_blockers: availabilityBlockers } : {}),
					routing_telemetry: routingTelemetry(),
				} satisfies RouteTaskDetails,
			};
		},
	});
}
