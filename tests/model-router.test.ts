import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import type { ExtensionAPI, ExtensionToolContext, ToolDefinition } from "@earendil-works/pi-coding-agent";
import { CONFIG_DIR_NAME } from "@earendil-works/pi-coding-agent";

import modelRouterExtension, { runJsonAdapter } from "../extensions/model-router.ts";

test("hung routing adapter terminates within its deadline", async () => {
  const dir = mkdtempSync(join(tmpdir(), "pico-router-timeout-"));
  try {
    const script = join(dir, "hang.py");
    writeFileSync(script, "import time\ntime.sleep(5)\n");
    const started = Date.now();
    const result = await runJsonAdapter(script, [], {}, undefined, 50);
    assert.match(result.spawnError?.message ?? "", /timed out/);
    assert.ok(Date.now() - started < 2000);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("unbounded routing adapter output is rejected before exhausting memory", async () => {
  const dir = mkdtempSync(join(tmpdir(), "pico-router-output-"));
  try {
    const script = join(dir, "flood.py");
    writeFileSync(script, "import sys\nsys.stdout.write('x' * (9 * 1024 * 1024))\n");
    const result = await runJsonAdapter(script, [], {}, undefined, 5000);
    assert.match(result.spawnError?.message ?? "", /output exceeded/);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

const REPO_ROOT = join(import.meta.dirname, "..");
const TASK_SCHEMA = JSON.parse(readFileSync(join(REPO_ROOT, "schemas", "task.schema.json"), "utf-8"));
const POLICY_TEXT = readFileSync(join(REPO_ROOT, "routing", "default-policy.yaml"), "utf-8");
const PROMPT_TEXT = readFileSync(join(REPO_ROOT, "prompts", "orchestrate.md"), "utf-8");

// Mirrors tests/routing-cases.yaml case-1-diagnosis-required.
const CASE_1_TASK = {
	id: "case-1-diagnosis-required",
	goal: "Fix a subtle race condition causing persisted metadata edits to disappear.",
	task_type: "debugging",
	failure_cost: "high",
	semantic_correctness: "normal",
	ambiguity: 5,
	implementation_scope: "small",
	implementation_required: true,
	cross_layer: false,
	behavior_preservation: "required",
	diagnosis_required: true,
	assumptions_must_be_explicit: true,
	many_routine_decisions: false,
	production_feature: false,
	security_sensitive: false,
	destructive_operation: false,
	escalation_evidence: {
		failure_cost: "Lost metadata edits silently destroy persisted user data.",
		behavior_preservation: "Existing persisted behavior must remain unchanged except for the race.",
		assumptions_must_be_explicit: "The race's trigger conditions are unverified and must be stated before fixing.",
	},
	dependencies: [] as string[],
	acceptance_criteria: ["Race condition no longer causes lost metadata edits."],
	files_or_areas: ["src/metadata/persistence"],
};

// Mirrors tests/routing-cases.yaml override-a-security-sensitive-floor.
const OVERRIDE_A_TASK = {
	id: "override-a-security-sensitive-floor",
	goal: "Improve spacing, layout and usability across the administration UI.",
	task_type: "ui_ux",
	failure_cost: "low",
	semantic_correctness: "normal",
	ambiguity: 2,
	implementation_scope: "small",
	implementation_required: true,
	cross_layer: false,
	behavior_preservation: "preferred",
	diagnosis_required: false,
	assumptions_must_be_explicit: false,
	many_routine_decisions: true,
	production_feature: false,
	security_sensitive: false,
	destructive_operation: false,
	dependencies: [] as string[],
	acceptance_criteria: ["Administration UI spacing and layout are visibly more consistent."],
	files_or_areas: ["src/admin/ui"],
};

const CRITICAL_IMPLEMENTATION_TASK = {
	id: "critical-implementation-reroute",
	goal: "Implement a security-sensitive cross-layer fix with independent verification.",
	task_type: "implementation",
	failure_cost: "medium",
	semantic_correctness: "critical",
	ambiguity: 2,
	implementation_scope: "medium",
	implementation_required: true,
	cross_layer: true,
	behavior_preservation: "required",
	diagnosis_required: false,
	assumptions_must_be_explicit: true,
	many_routine_decisions: true,
	production_feature: true,
	security_sensitive: true,
	destructive_operation: false,
	escalation_evidence: {
		semantic_correctness: "A wrong fix silently changes the protected contract semantics.",
		behavior_preservation: "Existing cross-layer behavior must stay exactly equivalent.",
		assumptions_must_be_explicit: "The threat model must be stated, not assumed.",
		security_sensitive: "Changes security-sensitive handling across layers.",
	},
	dependencies: [] as string[],
	acceptance_criteria: ["Fix is implemented and independently verified."],
	files_or_areas: ["src/security", "src/ui"],
};

// CASE_1_TASK's exact, real `route_task` output (produced by
// `tools/route_task.py`, policy v7): a single standalone CODEX worker step
// that keeps its authored standard tier because both downward candidate
// rules (`ops-light`, then `low-risk-worker`) are blocked by unmet guard
// predicates. Fake-adapter tests below clone this valid v7 result and mutate
// only the single field under test, so a regression in the specific
// condition each test names is what causes the rejection -- not an
// incidental, unrelated policy_version or capacity-assignment shape mismatch
// that would make the test pass for the wrong reason.
const CASE_1_BLOCKING_PREDICATES = [
	{ rule: "ops-light", field: "task_type", operator: "IN", value: ["vcs_operation", "issue_management", "config_edit", "documentation", "ci_fix"], actual: "debugging" },
	{ rule: "ops-light", field: "failure_cost", operator: "IN", value: ["low", "medium"], actual: "high" },
	{ rule: "ops-light", field: "diagnosis_required", operator: "==", value: false, actual: true },
	{ rule: "low-risk-worker", field: "failure_cost", operator: "==", value: "low", actual: "high" },
	{ rule: "low-risk-worker", field: "ambiguity", operator: "<=", value: 3, actual: 5 },
	{ rule: "low-risk-worker", field: "behavior_preservation", operator: "!=", value: "required", actual: "required" },
	{ rule: "low-risk-worker", field: "diagnosis_required", operator: "==", value: false, actual: true },
	{ rule: "low-risk-worker", field: "assumptions_must_be_explicit", operator: "==", value: false, actual: true },
];
const CASE_1_GUARD_PREDICATES_PASSED = [
	{ rule: "ops-light", field: "implementation_scope", operator: "==", value: "small", actual: "small" },
	{ rule: "ops-light", field: "ambiguity", operator: "<=", value: 6, actual: 5 },
	{ rule: "ops-light", field: "semantic_correctness", operator: "!=", value: "critical", actual: "normal" },
	{ rule: "ops-light", field: "security_sensitive", operator: "==", value: false, actual: false },
	{ rule: "ops-light", field: "destructive_operation", operator: "==", value: false, actual: false },
	{ rule: "low-risk-worker", field: "semantic_correctness", operator: "!=", value: "critical", actual: "normal" },
	{ rule: "low-risk-worker", field: "implementation_scope", operator: "==", value: "small", actual: "small" },
	{ rule: "low-risk-worker", field: "cross_layer", operator: "==", value: false, actual: false },
	{ rule: "low-risk-worker", field: "production_feature", operator: "==", value: false, actual: false },
	{ rule: "low-risk-worker", field: "security_sensitive", operator: "==", value: false, actual: false },
	{ rule: "low-risk-worker", field: "destructive_operation", operator: "==", value: false, actual: false },
];

function blockedStandardAssignment(family: "codex" | "claude", stepId: string) {
	const model = family === "codex" ? "openai-codex/gpt-6-luna" : "sonnet";
	return {
		step_id: stepId,
		authored_capacity_tier: "standard",
		assigned_capacity_tier: "standard",
		rule: null,
		decision_reason:
			"authored tier kept: ops-light blocked by 3 unmet guard predicate(s); " +
			"low-risk-worker blocked by 5 unmet guard predicate(s)",
		guard_predicates_passed: CASE_1_GUARD_PREDICATES_PASSED,
		blocking_predicates: CASE_1_BLOCKING_PREDICATES,
		selected_model_profile: { family, capacity_tier: "standard", model, context_window_tier: "standard", ambiguity_capacity: 6 },
	};
}

const CASE_1_VALID_RESULT = {
	ok: true,
	policy_version: 7,
	selected_route: "CODEX",
	matches: { base: ["CODEX"], effective: ["CODEX"] },
	fallback_applied: false,
	classification: { base: CASE_1_TASK, effective: CASE_1_TASK },
	project_constraints: {},
	capacity_assignments: [blockedStandardAssignment("codex", "codex_worker")],
	backend_bindings: {
		codex: { tool: "spawn_subagent", capacity_models: { standard: { model: "openai-codex/gpt-6-luna", context_window_tier: "standard", ambiguity_capacity: 6 } } },
	},
	execution_plan: {
		schema_version: 3,
		independent_review_required: false,
		steps: [
			{
				id: "codex_worker",
				order: 1,
				role: "worker",
				family: "codex",
				intent: "implementation",
				capacity_tier: "standard",
				reasoning_effort: "high",
				context_strategy: "full",
				fresh_context: true,
				mutation_permission: "allowed",
				depends_on: [],
				independent_review: false,
			},
		],
	},
};

function cloneValidResult(): Record<string, unknown> {
	return JSON.parse(JSON.stringify(CASE_1_VALID_RESULT));
}

function captureRegisteredTool(): { pi: ExtensionAPI; getTool: () => ToolDefinition; getCallCount: () => number } {
	let captured: ToolDefinition | undefined;
	let callCount = 0;
	const pi = {
		registerTool: (tool: ToolDefinition) => {
			callCount += 1;
			captured = tool;
		},
	} as unknown as ExtensionAPI;
	return {
		pi,
		getTool: () => {
			assert.ok(captured, "expected registerTool to have been called");
			return captured!;
		},
		getCallCount: () => callCount,
	};
}

function fakeContext(cwd: string): ExtensionToolContext {
	return { cwd } as unknown as ExtensionToolContext;
}

test("registers exactly one route_task tool", () => {
	const { pi, getTool, getCallCount } = captureRegisteredTool();

	modelRouterExtension(pi);

	assert.equal(getCallCount(), 1);
	assert.equal(getTool().name, "route_task");
});

test("exposes the canonical task.schema.json, not a hand-written subset", () => {
	const { pi, getTool } = captureRegisteredTool();
	modelRouterExtension(pi);
	const tool = getTool();

	const params = tool.parameters as unknown as {
		properties: { classified_task: Record<string, unknown> };
	};
	const exposed = params.properties.classified_task;

	assert.deepEqual(exposed.required, TASK_SCHEMA.required);
	assert.deepEqual(exposed.properties, TASK_SCHEMA.properties);
	assert.equal(exposed.additionalProperties, TASK_SCHEMA.additionalProperties);
});

test("CONFIG_DIR_NAME is the constant used to resolve project-local routing config", () => {
	// Guards against the extension (or this test) hardcoding ".pi" independently
	// of the package's own constant.
	assert.equal(CONFIG_DIR_NAME, ".pi");
});

test("executes route_task through the real subprocess boundary and content/details agree", async () => {
	const { pi, getTool } = captureRegisteredTool();
	modelRouterExtension(pi);
	const tool = getTool();

	const cwd = mkdtempSync(join(tmpdir(), "model-router-test-"));
	try {
		const result = await tool.execute(
			"call-1",
			{ classified_task: CASE_1_TASK } as never,
			undefined,
			undefined,
			fakeContext(cwd),
		);

		assert.equal(result.content.length, 1);
		assert.equal(result.content[0].type, "text");
		const fromContent = JSON.parse((result.content[0] as { text: string }).text);
		const details = result.details as { ok: boolean; result?: Record<string, unknown>; routing_telemetry?: Record<string, unknown> };

		assert.equal(details.ok, true);
		assert.equal(details.routing_telemetry?.source, "route_task");
		assert.equal(details.routing_telemetry?.schema_version, 1);
		assert.equal(typeof details.routing_telemetry?.duration_ms, "number");
		assert.equal(details.routing_telemetry?.input_tokens, null);
		assert.equal(details.routing_telemetry?.cached_tokens, null);
		assert.deepEqual(fromContent, details.result);
		assert.equal(details.result?.policy_version, 7);
		assert.equal(details.result?.selected_route, "CODEX");
		const assignments = details.result?.capacity_assignments as Array<Record<string, unknown>>;
		assert.equal(assignments.length, 1);
		assert.equal(assignments[0].step_id, "codex_worker");
		assert.equal(assignments[0].authored_capacity_tier, "standard");
		assert.equal(assignments[0].assigned_capacity_tier, "standard");
		assert.equal(assignments[0].rule, null);
		assert.equal(details.result?.fallback_applied, false);
		assert.deepEqual((details.result as { matches: { base: string[] } }).matches.base, ["CODEX"]);
		const plan = details.result?.execution_plan as { schema_version: number; steps: Record<string, unknown>[] };
		assert.equal(plan.schema_version, 3);
		assert.deepEqual(
			Object.fromEntries(["capacity_tier", "reasoning_effort", "context_strategy"].map((key) => [key, plan.steps[0][key]])),
			{ capacity_tier: "standard", reasoning_effort: "high", context_strategy: "full" },
		);
		const binding = (details.result?.backend_bindings as { codex: { capacity_models: Record<string, unknown> } }).codex;
		assert.deepEqual(Object.keys(binding.capacity_models), ["standard"]);
	} finally {
		rmSync(cwd, { recursive: true, force: true });
	}
});

test("escalating values without escalation_evidence surface CLASSIFICATION_EVIDENCE_MISSING", async () => {
	const { pi, getTool } = captureRegisteredTool();
	modelRouterExtension(pi);
	const tool = getTool();

	const { escalation_evidence: _evidence, ...unjustified } = CASE_1_TASK;
	const cwd = mkdtempSync(join(tmpdir(), "model-router-test-"));
	try {
		const result = await tool.execute(
			"call-evidence",
			{ classified_task: unjustified } as never,
			undefined,
			undefined,
			fakeContext(cwd),
		);
		const text = (result.content[0] as { text: string }).text;
		const details = result.details as { ok: boolean; blocker_code?: string; result?: Record<string, unknown> };
		assert.equal(details.ok, false);
		assert.equal(details.blocker_code, "CLASSIFICATION_EVIDENCE_MISSING");
		assert.deepEqual(details.result?.missing_evidence_fields,
			["failure_cost", "behavior_preservation", "assumptions_must_be_explicit"]);
		assert.equal(details.result?.selected_route, undefined);
		assert.match(text, /CLASSIFICATION_EVIDENCE_MISSING/);
		assert.match(text, /failure_cost/);
	} finally {
		rmSync(cwd, { recursive: true, force: true });
	}
});

test("unavailable preferred route is preflighted and rerouted before returning", async () => {
	for (const code of ["MODEL_BACKEND_UNAVAILABLE", "MODEL_AUTH_UNAVAILABLE", "MODEL_BACKEND_VERSION_UNSUPPORTED"]) {
		const { details, routeCalls } = await routeWithFakePreflight(CRITICAL_IMPLEMENTATION_TASK,
			{ CODEX_CLAUDE_CODEX: blocker(code) });
		assert.equal(details.ok, true, details.error);
		assert.equal(details.result?.selected_route, "CODEX");
		assert.deepEqual(details.result?.route_availability.excluded_routes, ["CODEX_CLAUDE_CODEX"]);
		assert.equal(details.result?.execution_plan.steps[0].id, "codex_worker");
		assert.deepEqual(details.availability_blockers, [{ route: "CODEX_CLAUDE_CODEX", blocker_code: code }]);
		assert.equal(routeCalls, 2);
	}
});

test("backend identity mismatch fails closed without rerouting", async () => {
	const { details, text, routeCalls } = await routeWithFakePreflight(CRITICAL_IMPLEMENTATION_TASK,
		{ CODEX_CLAUDE_CODEX: blocker("MODEL_BACKEND_MISMATCH") });
	assert.equal(details.ok, false);
	assert.equal(details.blocker_code, "MODEL_BACKEND_MISMATCH");
	assert.equal(details.result, undefined, "no route may be returned");
	assert.match(text, /MODEL_BACKEND_MISMATCH/);
	assert.equal(routeCalls, 1);
});

test("uninitialized backend identity fails closed with launcher guidance", async () => {
	const remediation = "Start Pi through a canonical launcher: scripts/use-latest.sh run <target-repo> or scripts/run-local.sh <target-repo>.";
	const { details, text, routeCalls } = await routeWithFakePreflight(CRITICAL_IMPLEMENTATION_TASK,
		{ CODEX_CLAUDE_CODEX: blocker("BACKEND_IDENTITY_UNINITIALIZED", { identity_state: "missing", remediation }) });
	assert.equal(details.ok, false);
	assert.equal(details.blocker_code, "BACKEND_IDENTITY_UNINITIALIZED");
	assert.match(text, /scripts\/use-latest\.sh/);
	assert.match(text, /scripts\/run-local\.sh/);
	assert.equal(routeCalls, 1);
});

test("unstructured backend preflight output fails closed", async () => {
	for (const output of ["not json", "{}", '{"ok":false}']) {
		const { details, routeCalls } = await routeWithFakePreflight(CRITICAL_IMPLEMENTATION_TASK,
			{ CODEX_CLAUDE_CODEX: output });
		assert.equal(details.ok, false, output);
		assert.equal(details.result, undefined);
		assert.equal(routeCalls, 1);
	}
});

test("no remaining policy-valid candidate fails closed as NO_AVAILABLE_ROUTE", async () => {
	const unavailable = blocker("MODEL_BACKEND_UNAVAILABLE");
	const { details, text, routeCalls } = await routeWithFakePreflight(CRITICAL_IMPLEMENTATION_TASK,
		{ CODEX_CLAUDE_CODEX: unavailable, CODEX: unavailable });
	assert.equal(details.ok, false);
	assert.equal(details.result?.blocker_code, "NO_AVAILABLE_ROUTE");
	assert.equal(details.result?.selected_route, undefined);
	assert.match(text, /NO_AVAILABLE_ROUTE/);
	assert.deepEqual(details.availability_blockers.map((item: { route: string }) => item.route), ["CODEX_CLAUDE_CODEX", "CODEX"]);
	assert.equal(routeCalls, 3);
});

test("large schema-valid task routes successfully without malformed-output failure", async () => {
	const { pi, getTool } = captureRegisteredTool();
	modelRouterExtension(pi);
	const tool = getTool();

	const cwd = mkdtempSync(join(tmpdir(), "model-router-test-"));
	try {
		const largeTask = { ...CASE_1_TASK, id: "large-task", goal: "x".repeat(300_000) };
		const result = await tool.execute(
			"call-large",
			{ classified_task: largeTask } as never,
			undefined,
			undefined,
			fakeContext(cwd),
		);

		const contentText = (result.content[0] as { text: string }).text;
		const fromContent = JSON.parse(contentText) as Record<string, unknown>;
		const details = result.details as { ok: boolean; result?: Record<string, unknown>; error?: string };
		assert.equal(details.ok, true, details.error);
		assert.equal(details.result?.selected_route, "CODEX");
		assert.equal(fromContent.selected_route, "CODEX");
		assert.deepEqual(fromContent.execution_plan, details.result?.execution_plan);
		assert.equal(((details.result?.classification as { base: { goal: string } }).base.goal).length, 300_000);
		assert.equal(fromContent.classification, undefined);
		assert.equal(fromContent.content_truncated, true);
		assert.ok(Buffer.byteLength(contentText, "utf-8") <= 512 * 1024);
	} finally {
		rmSync(cwd, { recursive: true, force: true });
	}
});

test("low-risk Claude result preserves post-route economy capacity assignment", async () => {
	const { pi, getTool } = captureRegisteredTool();
	modelRouterExtension(pi);
	const tool = getTool();
	const cwd = mkdtempSync(join(tmpdir(), "model-router-test-"));
	try {
		const result = await tool.execute(
			"call-economy",
			{ classified_task: OVERRIDE_A_TASK } as never,
			undefined,
			undefined,
			fakeContext(cwd),
		);
		const details = result.details as { ok: boolean; result?: Record<string, unknown> };
		assert.equal(details.ok, true);
		assert.equal(details.result?.selected_route, "CLAUDE");
		const assignments = details.result?.capacity_assignments as Array<Record<string, unknown>>;
		assert.equal(assignments[0].assigned_capacity_tier, "economy");
		assert.equal(assignments[0].rule, "low-risk-worker");
		const bindings = details.result?.backend_bindings as Record<string, { capacity_models: Record<string, unknown> }>;
		assert.deepEqual(Object.keys(bindings.claude.capacity_models), ["economy"]);
	} finally {
		rmSync(cwd, { recursive: true, force: true });
	}
});

test("orchestration prompt does not hard-code configured model identifiers", () => {
	const models = [...POLICY_TEXT.matchAll(/^\s+model:\s+(\S+)\s*$/gm)].map((match) => match[1]);
	assert.ok(models.length >= 3);
	for (const model of new Set(models)) assert.equal(PROMPT_TEXT.includes(model), false, model);
});

test("joins ctx.cwd + CONFIG_DIR_NAME to load project-local risk_overrides", async () => {
	const { pi, getTool } = captureRegisteredTool();
	modelRouterExtension(pi);
	const tool = getTool();

	const cwd = mkdtempSync(join(tmpdir(), "model-router-test-"));
	try {
		mkdirSync(join(cwd, CONFIG_DIR_NAME), { recursive: true });
		writeFileSync(
			join(cwd, CONFIG_DIR_NAME, "routing.yaml"),
			"risk_overrides:\n  security_sensitive: true\n",
			"utf-8",
		);

		const result = await tool.execute(
			"call-2",
			{ classified_task: OVERRIDE_A_TASK } as never,
			undefined,
			undefined,
			fakeContext(cwd),
		);

		const details = result.details as { ok: boolean; result?: Record<string, unknown> };
		assert.equal(details.ok, true);
		// Without the override this task routes to CLAUDE; the security_sensitive
		// floor from .pi/routing.yaml must escalate it to CODEX_CLAUDE_CODEX.
		assert.equal(details.result?.selected_route, "CODEX_CLAUDE_CODEX");
		const bindings = details.result?.backend_bindings as Record<string, { capacity_models: Record<string, unknown> }>;
		assert.deepEqual(Object.keys(bindings.claude.capacity_models), ["high"]);
		assert.deepEqual(Object.keys(bindings.codex.capacity_models), ["high"]);
	} finally {
		rmSync(cwd, { recursive: true, force: true });
	}
});

test("large project constraints keep LLM-visible content bounded and route-critical", async () => {
	const { pi, getTool } = captureRegisteredTool();
	modelRouterExtension(pi);
	const tool = getTool();

	const cwd = mkdtempSync(join(tmpdir(), "model-router-test-"));
	try {
		mkdirSync(join(cwd, CONFIG_DIR_NAME), { recursive: true });
		writeFileSync(join(cwd, CONFIG_DIR_NAME, "routing.yaml"), `notes:\n  - ${"x".repeat(600_000)}\n`, "utf-8");

		const result = await tool.execute(
			"call-large-project",
			{ classified_task: CASE_1_TASK } as never,
			undefined,
			undefined,
			fakeContext(cwd),
		);

		const contentText = (result.content[0] as { text: string }).text;
		const fromContent = JSON.parse(contentText) as Record<string, unknown>;
		const details = result.details as { ok: boolean; result?: Record<string, unknown> };
		assert.equal(details.ok, true);
		assert.equal(fromContent.selected_route, "CODEX");
		assert.deepEqual(fromContent.execution_plan, details.result?.execution_plan);
		assert.deepEqual(fromContent.capacity_assignments, details.result?.capacity_assignments);
		assert.deepEqual(fromContent.backend_bindings, details.result?.backend_bindings);
		assert.equal(fromContent.project_constraints, undefined);
		assert.equal(fromContent.content_truncated, true);
		assert.ok(Buffer.byteLength(contentText, "utf-8") <= 512 * 1024);
	} finally {
		rmSync(cwd, { recursive: true, force: true });
	}
});

test("an invalid task returns ok:false with no route, not a tool error", async () => {
	const { pi, getTool } = captureRegisteredTool();
	modelRouterExtension(pi);
	const tool = getTool();

	const cwd = mkdtempSync(join(tmpdir(), "model-router-test-"));
	try {
		const result = await tool.execute(
			"call-3",
			{ classified_task: { id: "incomplete" } } as never,
			undefined,
			undefined,
			fakeContext(cwd),
		);

		const details = result.details as { ok: boolean; result?: { ok: boolean; errors?: unknown[] } };
		assert.equal(details.ok, false);
		assert.ok(Array.isArray(details.result?.errors));
		assert.ok((details.result?.errors as unknown[]).length > 0);
		assert.equal((details.result as Record<string, unknown>).selected_route, undefined);
	} finally {
		rmSync(cwd, { recursive: true, force: true });
	}
});

test("cancellation before execution short-circuits without spawning", async () => {
	const { pi, getTool } = captureRegisteredTool();
	modelRouterExtension(pi);
	const tool = getTool();

	const cwd = mkdtempSync(join(tmpdir(), "model-router-test-"));
	try {
		const controller = new AbortController();
		controller.abort();

		const result = await tool.execute(
			"call-4",
			{ classified_task: CASE_1_TASK } as never,
			controller.signal,
			undefined,
			fakeContext(cwd),
		);

		const details = result.details as { ok: boolean; error?: string };
		assert.equal(details.ok, false);
		assert.equal(details.error, "cancelled");
	} finally {
		rmSync(cwd, { recursive: true, force: true });
	}
});

test("a nonzero adapter exit surfaces as a tool-level failure, not a route", async () => {
	const { pi, getTool } = captureRegisteredTool();
	modelRouterExtension(pi);
	const tool = getTool();

	const cwd = mkdtempSync(join(tmpdir(), "model-router-test-"));
	try {
		// An existing-but-unreadable project-routing path (a directory instead of
		// a file) makes tools/route_task.py exit nonzero (operational failure).
		mkdirSync(join(cwd, CONFIG_DIR_NAME), { recursive: true });
		mkdirSync(join(cwd, CONFIG_DIR_NAME, "routing.yaml"), { recursive: true });

		const result = await tool.execute(
			"call-5",
			{ classified_task: CASE_1_TASK } as never,
			undefined,
			undefined,
			fakeContext(cwd),
		);

		const details = result.details as { ok: boolean; error?: string; routing_telemetry?: Record<string, unknown> };
		assert.equal(details.ok, false);
		assert.ok(details.error?.includes("exited with code"));
		assert.equal(details.routing_telemetry?.source, "route_task");
		assert.equal(typeof details.routing_telemetry?.duration_ms, "number");
	} finally {
		rmSync(cwd, { recursive: true, force: true });
	}
});

function writeFakeAdapterScript(dir: string, stdoutJson: string): string {
	const scriptPath = join(dir, "fake-adapter.sh");
	writeFileSync(
		scriptPath,
		`#!/bin/sh\ncase "$1" in\n  *validate_route_result.py) exec python3 "$@" ;;\nesac\ncat <<'PAYLOAD_EOF'\n${stdoutJson}\nPAYLOAD_EOF\n`,
		"utf-8",
	);
	chmodSync(scriptPath, 0o755);
	return scriptPath;
}

function writeDelayedValidatorAdapterScript(dir: string, stdoutJson: string): string {
	const scriptPath = join(dir, "fake-delayed-validator-adapter.sh");
	writeFileSync(
		scriptPath,
		`#!/bin/sh
case "$1" in
  *validate_route_result.py) sleep 0.2; exec python3 "$@" ;;
esac
cat <<'PAYLOAD_EOF'
${stdoutJson}
PAYLOAD_EOF
`,
		"utf-8",
	);
	chmodSync(scriptPath, 0o755);
	return scriptPath;
}

/**
 * Fake interpreter: route_task/validator run for real; backend_preflight returns
 * `blocked[route]` (exit 1) for the listed routes, or `fallthrough` otherwise.
 * Every route_task invocation is appended to `<dir>/route-calls.log`.
 */
function writeFakePreflightScript(dir: string, blocked: Record<string, string>, fallthrough = '{"ok":true,"status":"READY"}'): string {
	const scriptPath = join(dir, "fake-preflight.sh");
	const cases = Object.entries(blocked).map(([route, output]) =>
		`    if printf '%s' "$input" | grep -q '"selected_route"[[:space:]]*:[[:space:]]*"${route}"'; then\n` +
		`      printf '%s\\n' '${output}'\n      exit 1\n    fi\n`).join("");
	writeFileSync(
		scriptPath,
		`#!/bin/sh
case "$1" in
  *backend_preflight.py)
    input=$(cat)
${cases}    printf '%s\\n' '${fallthrough}'
    case '${fallthrough}' in *'"ok":true'*) exit 0 ;; *) exit 1 ;; esac
    ;;
  *route_task.py) echo call >> "${join(dir, "route-calls.log")}"; exec python3 "$@" ;;
  *) exec python3 "$@" ;;
esac
`,
		"utf-8",
	);
	chmodSync(scriptPath, 0o755);
	return scriptPath;
}

function blocker(code: string, extra: Record<string, string> = {}): string {
	return JSON.stringify({ ok: false, status: "BLOCKED", blocker_code: code, step_id: "claude_worker",
		backend: "claude", model: "anthropic/claude-opus-5-5", ...extra });
}

async function routeWithFakePreflight(
	task: Record<string, unknown>,
	blocked: Record<string, string>,
	fallthrough?: string,
): Promise<{ details: Record<string, any>; text: string; routeCalls: number }> {
	const previous = process.env.PI_ROUTER_PYTHON;
	const scriptDir = mkdtempSync(join(tmpdir(), "model-router-preflight-"));
	const cwd = mkdtempSync(join(tmpdir(), "model-router-test-"));
	try {
		process.env.PI_ROUTER_PYTHON = writeFakePreflightScript(scriptDir, blocked, fallthrough);
		let captured: ToolDefinition | undefined;
		const pi = {
			registerTool: (tool: ToolDefinition) => { captured = tool; },
			getActiveTools: () => ["route_task", "spawn_subagent", "ClaudeWorker", "orchestration_record"],
		} as unknown as ExtensionAPI;
		modelRouterExtension(pi);
		assert.ok(captured);
		const result = await captured.execute("call-preflight", { classified_task: task } as never,
			undefined, undefined, fakeContext(cwd));
		const log = join(scriptDir, "route-calls.log");
		const routeCalls = readFileSync(log, "utf-8").trim().split("\n").filter(Boolean).length;
		return { details: result.details as Record<string, any>, text: (result.content[0] as { text: string }).text, routeCalls };
	} finally {
		rmSync(scriptDir, { recursive: true, force: true });
		rmSync(cwd, { recursive: true, force: true });
		if (previous === undefined) delete process.env.PI_ROUTER_PYTHON;
		else process.env.PI_ROUTER_PYTHON = previous;
	}
}

async function withFakeAdapterOutput(
	stdoutJson: string,
	run: (tool: ToolDefinition, cwd: string) => Promise<void>,
): Promise<void> {
	const previous = process.env.PI_ROUTER_PYTHON;
	const scriptDir = mkdtempSync(join(tmpdir(), "model-router-fake-adapter-"));
	const cwd = mkdtempSync(join(tmpdir(), "model-router-test-"));
	try {
		process.env.PI_ROUTER_PYTHON = writeFakeAdapterScript(scriptDir, stdoutJson);
		const { pi, getTool } = captureRegisteredTool();
		modelRouterExtension(pi);
		await run(getTool(), cwd);
	} finally {
		rmSync(scriptDir, { recursive: true, force: true });
		rmSync(cwd, { recursive: true, force: true });
		if (previous === undefined) {
			delete process.env.PI_ROUTER_PYTHON;
		} else {
			process.env.PI_ROUTER_PYTHON = previous;
		}
	}
}

test("route_task telemetry duration includes successful result validation boundary", async () => {
	const previous = process.env.PI_ROUTER_PYTHON;
	const scriptDir = mkdtempSync(join(tmpdir(), "model-router-delayed-validator-"));
	const cwd = mkdtempSync(join(tmpdir(), "model-router-test-"));
	try {
		process.env.PI_ROUTER_PYTHON = writeDelayedValidatorAdapterScript(scriptDir, JSON.stringify(cloneValidResult()));
		const { pi, getTool } = captureRegisteredTool();
		modelRouterExtension(pi);
		const result = await getTool().execute(
			"call-delayed-validator",
			{ classified_task: CASE_1_TASK } as never,
			undefined,
			undefined,
			fakeContext(cwd),
		);
		const details = result.details as { routing_telemetry?: Record<string, unknown> };
		assert.equal(details.routing_telemetry?.source, "route_task");
		assert.equal(typeof details.routing_telemetry?.duration_ms, "number");
		assert.ok((details.routing_telemetry?.duration_ms as number) >= 100);
	} finally {
		rmSync(scriptDir, { recursive: true, force: true });
		rmSync(cwd, { recursive: true, force: true });
		if (previous === undefined) {
			delete process.env.PI_ROUTER_PYTHON;
		} else {
			process.env.PI_ROUTER_PYTHON = previous;
		}
	}
});

test("ok:true route_task output with telemetry fields is rejected at the subprocess boundary", async () => {
	const fakeResult = JSON.stringify({ ...cloneValidResult(), telemetry: { duration_ms: 1 } });

	await withFakeAdapterOutput(fakeResult, async (tool, cwd) => {
		const result = await tool.execute(
			"call-telemetry-result",
			{ classified_task: CASE_1_TASK } as never,
			undefined,
			undefined,
			fakeContext(cwd),
		);

		const details = result.details as { ok: boolean; result?: unknown; error?: string };
		assert.equal(details.ok, false);
		assert.equal(details.result, undefined);
		assert.ok(details.error?.includes("telemetry"));
	});
});

test("ok:true with a missing execution_plan is rejected at the subprocess boundary", async () => {
	const fake = cloneValidResult();
	delete fake.execution_plan;

	await withFakeAdapterOutput(JSON.stringify(fake), async (tool, cwd) => {
		const result = await tool.execute(
			"call-missing-plan",
			{ classified_task: CASE_1_TASK } as never,
			undefined,
			undefined,
			fakeContext(cwd),
		);

		const details = result.details as { ok: boolean; result?: unknown; error?: string };
		assert.equal(details.ok, false);
		assert.equal(details.result, undefined);
		assert.ok(details.error?.includes("execution_plan"));
	});
});

test("ok:true with a schema-invalid execution_plan is rejected at the subprocess boundary", async () => {
	const fake = cloneValidResult() as { execution_plan: { steps: Array<Record<string, unknown>> } };
	// Invalid: role must be one of specifier/worker/reviewer.
	fake.execution_plan.steps[0].role = "not_a_real_role";

	await withFakeAdapterOutput(JSON.stringify(fake), async (tool, cwd) => {
		const result = await tool.execute(
			"call-invalid-plan",
			{ classified_task: CASE_1_TASK } as never,
			undefined,
			undefined,
			fakeContext(cwd),
		);

		const details = result.details as { ok: boolean; result?: unknown; error?: string };
		assert.equal(details.ok, false);
		assert.equal(details.result, undefined);
		assert.ok(details.error?.includes("execution_plan"));
	});
});

test("ok:true with a semantically invalid execution_plan is rejected at the subprocess boundary", async () => {
	const fake = cloneValidResult() as { execution_plan: { steps: Array<Record<string, unknown>> } };
	// Invalid: every authored top-level step's fresh_context must be true.
	fake.execution_plan.steps[0].fresh_context = false;

	await withFakeAdapterOutput(JSON.stringify(fake), async (tool, cwd) => {
		const result = await tool.execute(
			"call-invalid-semantic-plan",
			{ classified_task: CASE_1_TASK } as never,
			undefined,
			undefined,
			fakeContext(cwd),
		);

		const details = result.details as { ok: boolean; result?: unknown; error?: string };
		assert.equal(details.ok, false);
		assert.equal(details.result, undefined);
		assert.ok(details.error?.includes("semantically invalid"));
		assert.ok(details.error?.includes("result.execution_plan"));
	});
});

test("ok:true with type-corrupted effective classification is rejected", async () => {
	const fake = cloneValidResult() as { classification: { base: unknown; effective: Record<string, unknown> } };
	// Must not be accepted just because Python's loose dict equality would
	// treat True and 1 as equal; effective classifications are schema-validated
	// and compared with recursive, JSON type-sensitive equality.
	fake.classification.effective.implementation_required = 1;

	await withFakeAdapterOutput(JSON.stringify(fake), async (tool, cwd) => {
		const result = await tool.execute(
			"call-type-corrupted-effective",
			{ classified_task: CASE_1_TASK } as never,
			undefined,
			undefined,
			fakeContext(cwd),
		);

		const details = result.details as { ok: boolean; result?: unknown; error?: string };
		assert.equal(details.ok, false);
		assert.equal(details.result, undefined);
		assert.ok(details.error?.includes("semantically invalid"));
		assert.ok(details.error?.includes("result.classification.effective"));
	});
});

test("ok:true with claimed route disagreeing with deterministic routing is rejected", async () => {
	// Canonical standalone CLAUDE implementation plan/assignment for the
	// claimed route; invalid only because CASE_1_TASK deterministically
	// routes to CODEX.
	const fake = {
		...cloneValidResult(),
		selected_route: "CLAUDE",
		matches: { base: ["CLAUDE"], effective: ["CLAUDE"] },
		capacity_assignments: [blockedStandardAssignment("claude", "claude_worker")],
		execution_plan: {
			schema_version: 3,
			independent_review_required: false,
			steps: [
				{
					id: "claude_worker",
					order: 1,
					role: "worker",
					family: "claude",
					intent: "implementation",
					capacity_tier: "standard",
					reasoning_effort: "high",
					context_strategy: "full",
					fresh_context: true,
					mutation_permission: "allowed",
					depends_on: [],
					independent_review: false,
				},
			],
		},
		backend_bindings: {
			claude: { tool: "ClaudeWorker", capacity_models: { standard: { model: "sonnet", context_window_tier: "standard" } }, mutation_modes: { forbidden: "read", allowed: "full" } },
		},
	};

	await withFakeAdapterOutput(JSON.stringify(fake), async (tool, cwd) => {
		const result = await tool.execute(
			"call-claimed-route-disagrees",
			{ classified_task: CASE_1_TASK } as never,
			undefined,
			undefined,
			fakeContext(cwd),
		);

		const details = result.details as { ok: boolean; result?: unknown; error?: string };
		assert.equal(details.ok, false);
		assert.equal(details.result, undefined);
		assert.ok(details.error?.includes("semantically invalid"));
		assert.ok(details.error?.includes("result.selected_route"));
	});
});

test("ok:true with route-mismatched execution_plan is rejected at the subprocess boundary", async () => {
	// Structurally and semantically valid as a standalone one-step Claude
	// analysis plan/assignment, but not the canonical CODEX implementation
	// plan for CASE_1_TASK's effective classification.
	const fake = {
		...cloneValidResult(),
		capacity_assignments: [blockedStandardAssignment("claude", "claude_worker")],
		execution_plan: {
			schema_version: 3,
			independent_review_required: false,
			steps: [
				{
					id: "claude_worker",
					order: 1,
					role: "worker",
					family: "claude",
					intent: "analysis",
					capacity_tier: "standard",
					reasoning_effort: "high",
					context_strategy: "full",
					fresh_context: true,
					mutation_permission: "forbidden",
					depends_on: [],
					independent_review: false,
				},
			],
		},
		backend_bindings: {
			claude: { tool: "ClaudeWorker", capacity_models: { standard: { model: "sonnet", context_window_tier: "standard" } }, mutation_modes: { forbidden: "read", allowed: "full" } },
		},
	};

	await withFakeAdapterOutput(JSON.stringify(fake), async (tool, cwd) => {
		const result = await tool.execute(
			"call-route-mismatched-plan",
			{ classified_task: CASE_1_TASK } as never,
			undefined,
			undefined,
			fakeContext(cwd),
		);

		const details = result.details as { ok: boolean; result?: unknown; error?: string };
		assert.equal(details.ok, false);
		assert.equal(details.result, undefined);
		assert.ok(details.error?.includes("semantically invalid"));
		assert.ok(details.error?.includes("result.execution_plan"));
	});
});

test("ok:true with missing backend_bindings is rejected at the subprocess boundary", async () => {
	const fake = cloneValidResult();
	delete fake.backend_bindings;

	await withFakeAdapterOutput(JSON.stringify(fake), async (tool, cwd) => {
		const result = await tool.execute(
			"call-missing-bindings",
			{ classified_task: CASE_1_TASK } as never,
			undefined,
			undefined,
			fakeContext(cwd),
		);

		const details = result.details as { ok: boolean; result?: unknown; error?: string };
		assert.equal(details.ok, false);
		assert.equal(details.result, undefined);
		assert.ok(details.error?.includes("semantically invalid"));
		assert.ok(details.error?.includes("result.backend_bindings"));
	});
});

test("ok:true with forged capacity-assignment evidence is rejected at the subprocess boundary", async () => {
	// A forged selected_model_profile.model (evidence identifying which
	// concrete model was actually resolved for the assigned tier) must be
	// rejected even though every other field of the result is valid.
	const fake = cloneValidResult() as { capacity_assignments: Array<{ selected_model_profile: Record<string, unknown> }> };
	fake.capacity_assignments[0].selected_model_profile.model = "openai-codex/not-a-real-model";

	await withFakeAdapterOutput(JSON.stringify(fake), async (tool, cwd) => {
		const result = await tool.execute(
			"call-forged-capacity-evidence",
			{ classified_task: CASE_1_TASK } as never,
			undefined,
			undefined,
			fakeContext(cwd),
		);

		const details = result.details as { ok: boolean; result?: unknown; error?: string };
		assert.equal(details.ok, false);
		assert.equal(details.result, undefined);
		assert.ok(details.error?.includes("semantically invalid"));
		assert.ok(details.error?.includes("result.capacity_assignments"));
	});
});

test("malformed adapter output surfaces as a tool-level failure", async () => {
	const previous = process.env.PI_ROUTER_PYTHON;
	process.env.PI_ROUTER_PYTHON = "echo";
	try {
		const { pi, getTool } = captureRegisteredTool();
		modelRouterExtension(pi);
		const tool = getTool();

		const cwd = mkdtempSync(join(tmpdir(), "model-router-test-"));
		try {
			const result = await tool.execute(
				"call-6",
				{ classified_task: CASE_1_TASK } as never,
				undefined,
				undefined,
				fakeContext(cwd),
			);

			const details = result.details as { ok: boolean; error?: string };
			assert.equal(details.ok, false);
			assert.ok(details.error?.includes("malformed output"));
		} finally {
			rmSync(cwd, { recursive: true, force: true });
		}
	} finally {
		if (previous === undefined) {
			delete process.env.PI_ROUTER_PYTHON;
		} else {
			process.env.PI_ROUTER_PYTHON = previous;
		}
	}
});
