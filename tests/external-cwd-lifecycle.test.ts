import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { cpSync, existsSync, mkdtempSync, mkdirSync, readdirSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync, chmodSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { test } from "node:test";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const SOURCE = join(dirname(fileURLToPath(import.meta.url)), "..");

function run(command: string, args: string[], cwd: string, input?: string): string {
  const result = spawnSync(command, args, { cwd, input, encoding: "utf8" });
  assert.equal(result.status, 0, `${command} ${args[0]}: ${result.stderr || result.stdout}`);
  return result.stdout.trim();
}

test("loaded lifecycle uses installed orchestrator resources from external cwd", async () => {
  const base = mkdtempSync(join(tmpdir(), "pi-external-lifecycle-"));
  const installed = join(base, "arbitrary-installation");
  const target = join(base, "external-target");
  const previous = process.cwd();
  const previousRepo = process.env.PI_TARGET_REPOSITORY;
  delete process.env.PI_TARGET_REPOSITORY;
  try {
    for (const part of ["extensions", "tools", "schemas", "routing", "provenance/runs"]) mkdirSync(join(installed, part), { recursive: true });
    mkdirSync(target);
    for (const file of ["extensions/orchestration-lifecycle.ts", "extensions/claude-backend-adapter.ts",
      "extensions/claude-worker.ts",
      "extensions/context-budget.ts", "routing/backend-compatibility.json", "routing/context-policy.yaml", "routing/default-policy.yaml",
      "tools/parent_orchestration.py", "tools/context_budget.py", "tools/session_scope.py",
      "tools/routing_engine.py", "schemas/context-policy.schema.json", "schemas/context-checkpoint.schema.json",
      "tools/provenance.py", "tools/backlog_store.py", "tools/github_integration.py", "tools/github_sync.py",
      "tools/claude_backend.py", "schemas/run-record.schema.json", "schemas/task.schema.json"]) {
      cpSync(join(SOURCE, file), join(installed, file));
    }
    symlinkSync(join(SOURCE, "node_modules"), join(installed, "node_modules"), "dir");
    run("git", ["init", "-q", target], base);
    run("git", ["remote", "add", "origin", "git@github.com:Examplecorp/examplelab.git"], target);
    run("git", ["remote", "add", "oss", "https://github.com/Examplecorp/examplelab-oss.git"], target);
    const module = await import(pathToFileURL(join(installed, "extensions/orchestration-lifecycle.ts")).href);
    const handlers = new Map<string, (...args: any[]) => any>();
    const tools = new Map<string, any>();
    module.default({
      on(name: string, handler: (...args: any[]) => any) { handlers.set(name, handler); },
      registerTool(tool: any) { tools.set(tool.name, tool); },
      getActiveTools() { return ["route_task", "orchestration_record"]; },
    } as unknown as ExtensionAPI);
    assert.ok(tools.has("orchestration_record"));
    const recordTool = tools.get("orchestration_record");
    const context = { cwd: target, ui: { notify() {} } };
    process.chdir(target);

    // No explicit target is ambiguous. The parent persists a blocked record,
    // and all repository inspection or worker tools stay blocked.
    const blockedStart = await handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, context);
    assert.match(blockedStart.message.content, /BLOCKED/);
    const blockedStatus = await recordTool.execute("probe", { action: "status" });
    assert.equal(blockedStatus.details.ok, false);
    assert.equal(blockedStatus.details.result.blocker_code, "TARGET_REPOSITORY_AMBIGUOUS");
    assert.equal((await handlers.get("tool_call")!({ toolName: "bash", input: { command: "git status" } })).block, true);

    process.env.PI_TARGET_REPOSITORY = "Examplecorp/examplelab";
    await handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, context);
    const ready = await recordTool.execute("probe", { action: "status" });
    assert.equal(ready.details.ok, true);
    assert.equal(ready.details.result.target_repository, "Examplecorp/examplelab");
    const runId = ready.details.result.run_id;
    const recordPath = join(installed, "provenance/runs", `${runId}.json`);
    const initialBytes = readFileSync(recordPath, "utf8");
    const invalid = await recordTool.execute("event", { action: "event", event: {
      kind: "phase", phase: "preflight", status: "started",
    } });
    assert.equal(invalid.details.ok, false);
    assert.equal(invalid.details.result.error_code, "PARENT_PHASE_STATUS_INVALID");
    assert.equal(invalid.details.result.error, "invalid phase status");
    assert.equal(invalid.details.result.event_kind, "phase");
    assert.equal(invalid.details.result.lifecycle_state, "IN_PROGRESS");
    assert.equal(invalid.details.result.run_id, runId);
    assert.equal(readFileSync(recordPath, "utf8"), initialBytes);
    assert.equal((await handlers.get("tool_call")!({ toolName: "spawn_subagent", input: {} })).block, true);
    const recovered = await recordTool.execute("probe", { action: "status" });
    assert.equal(recovered.details.ok, true);
    assert.equal(await handlers.get("tool_call")!({ toolName: "spawn_subagent", input: {} }), undefined);
    const event = async (value: Record<string, unknown>) => {
      const result = await recordTool.execute("event", { action: "event", event: value });
      assert.equal(result.details.ok, true, JSON.stringify(result.details.result));
      return result;
    };
    await event({ kind: "phase", phase: "preflight", status: "in_progress" });
    await event({ kind: "phase", phase: "preflight", status: "completed" });
    await event({ kind: "phase", phase: "inspection", status: "in_progress" });
    await event({ kind: "phase", phase: "inspection", status: "completed" });
    await event({ kind: "phase", phase: "phase-a", status: "in_progress", route: "CODEX", policy_version: 4 });
    await event({ kind: "worker", phase: "phase-a", step_id: "codex_worker",
      model: "openai-codex/gpt-6-luna", status: "completed" });

    const child = JSON.parse(run("python3", ["-c", `import sys,json; sys.path.insert(0, ${JSON.stringify(join(installed, "tools"))}); import provenance; from pathlib import Path; r=provenance.start_run(Path(${JSON.stringify(installed)}),backlog_id=None,github_issue=401,user_objective='Read backlog',route_task_result=None,starting_commit=None); print(json.dumps({'run_id':r['run_id']}))`], target));
    await event({ kind: "child", phase: "phase-a", run_id: child.run_id });
    await event({ kind: "child", phase: "phase-a", run_id: child.run_id });
    await event({ kind: "phase", phase: "phase-a", status: "completed", route: "CODEX" });
    await event({ kind: "phase", phase: "s1", status: "in_progress", route: "CODEX_CLAUDE_CODEX" });
    await event({ kind: "phase", phase: "s1", status: "blocked",
      blocker_code: "MODEL_BACKEND_VERSION_UNSUPPORTED" });
    const blockedFinal = await recordTool.execute("finish", { action: "finish", status: "BLOCKED",
      blocker_code: "MODEL_BACKEND_VERSION_UNSUPPORTED" });
    assert.equal(blockedFinal.details.ok, true);
    const record = JSON.parse(readFileSync(recordPath, "utf8"));
    const phaseA = record.parent_orchestration.phases.find((phase: any) => phase.id === "phase-a");
    assert.equal(phaseA.child_run_ids[0], child.run_id);
    assert.equal(phaseA.child_run_ids.length, 1);
    assert.equal(record.parent_orchestration.final_status, "BLOCKED");

    await handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, context);
    const second = await recordTool.execute("probe", { action: "status" });
    assert.equal(second.details.ok, true);
    await event({ kind: "phase", phase: "preflight", status: "in_progress" });
    await event({ kind: "phase", phase: "preflight", status: "completed" });
    await event({ kind: "phase", phase: "inspection", status: "in_progress" });
    await event({ kind: "phase", phase: "inspection", status: "completed" });
    await event({ kind: "phase", phase: "phase-a", status: "in_progress", route: "CODEX", policy_version: 4 });
    await event({ kind: "worker", phase: "phase-a", step_id: "codex_worker",
      model: "openai-codex/gpt-6-luna", status: "completed" });
    await event({ kind: "phase", phase: "phase-a", status: "completed", route: "CODEX" });
    await event({ kind: "phase", phase: "s1", status: "in_progress", route: "CODEX_CLAUDE_CODEX" });
    await event({ kind: "worker", phase: "s1", step_id: "claude_worker", status: "completed",
      model: "anthropic/claude-opus-5-5", backend_model: "claude-opus-5-5" });
    await event({ kind: "phase", phase: "s1", status: "completed", route: "CODEX_CLAUDE_CODEX" });
    // The route's independent review is mandatory before COMPLETED.
    const unreviewed = await recordTool.execute("finish", { action: "finish", status: "COMPLETED" });
    assert.equal(unreviewed.details.ok, false);
    assert.match(unreviewed.content[0].text, /PARENT_REVIEW_MISSING/);
    assert.equal((await recordTool.execute("recheck", { action: "status" })).details.ok, true);
    await event({ kind: "worker", phase: "s1", step_id: "codex_reviewer", status: "completed",
      model: "openai-codex/gpt-6-luna" });
    const completed = await recordTool.execute("finish", { action: "finish", status: "COMPLETED" });
    assert.equal(completed.details.ok, true);
    const completeRecord = JSON.parse(readFileSync(join(installed, "provenance/runs", `${second.details.result.run_id}.json`), "utf8"));
    assert.equal(completeRecord.parent_orchestration.final_status, "COMPLETED");
    const claudeWorker = completeRecord.parent_orchestration.phases.find((phase: any) => phase.id === "s1").workers[0];
    assert.equal(claudeWorker.model, "anthropic/claude-opus-5-5");
    assert.equal(claudeWorker.backend_model, "claude-opus-5-5");
    rmSync(join(installed, "schemas/run-record.schema.json"));
    const missingSchema = await handlers.get("before_agent_start")!({ prompt: "PI_ORCHESTRATION_RUN_V1" }, context);
    assert.match(missingSchema.message.content, /BLOCKED/);
    const unavailable = await recordTool.execute("probe", { action: "status" });
    assert.equal(unavailable.details.ok, false);
    assert.equal(unavailable.details.result.error_category, "FileNotFoundError");
    const installedReal = realpathSync(installed);
    assert.equal(unavailable.details.result.orchestrator_root, installedReal);
    assert.equal(unavailable.details.result.resource_path, join(installedReal, "schemas/run-record.schema.json"));
    assert.equal((await handlers.get("tool_call")!({ toolName: "spawn_subagent", input: {} })).block, true);
    assert.deepEqual(readdirSync(target).sort(), [".git"]);
    assert.equal(existsSync(join(target, "provenance")), false);
    assert.equal(existsSync(join(target, "schemas")), false);
  } finally {
    process.chdir(previous);
    if (previousRepo === undefined) delete process.env.PI_TARGET_REPOSITORY;
    else process.env.PI_TARGET_REPOSITORY = previousRepo;
    rmSync(base, { recursive: true, force: true });
  }
});

test("canonical launcher loads every extension from a local cache and preserves external cwd", () => {
  const base = mkdtempSync(join(tmpdir(), "pi-launcher-topology-"));
  try {
    const seed = join(base, "seed");
    const target = join(base, "external-target");
    const binary = join(base, "bin");
    const cache = join(base, "cache");
    const bare = join(base, "origin.git");
    mkdirSync(seed); mkdirSync(target); mkdirSync(binary);
    for (const part of ["extensions", "tools", "schemas", "routing", "prompts", "scripts"]) {
      cpSync(join(SOURCE, part), join(seed, part), { recursive: true });
    }
    // The cached checkout must model the repository's ignore contract as well
    // as its runtime directories. Python imports create __pycache__/*.pyc;
    // omitting .gitignore from this fixture makes those normal runtime files
    // look like unsafe arbitrary cache dirt.
    cpSync(join(SOURCE, ".gitignore"), join(seed, ".gitignore"));
    run("git", ["init", "-q", "-b", "main", seed], base);
    run("git", ["add", "."], seed);
    run("git", ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "fixture"], seed);
    run("git", ["clone", "-q", "--bare", seed, bare], base);
    const fakePi = join(binary, "pi");
    writeFileSync(fakePi, "#!/usr/bin/env bash\nprintf 'CWD=%s\\nREPO=%s\\n' \"$PWD\" \"$PI_TARGET_REPOSITORY\"\nprintf 'ARG=%s\\n' \"$@\"\n");
    chmodSync(fakePi, 0o755);
    // Hermetic Claude: the launcher pins this executable; anything but a probe fails.
    const fakeClaude = join(binary, "claude");
    writeFileSync(fakeClaude, "#!/bin/sh\nif [ \"$1\" = --version ]; then echo '2.1.287 (Claude Code)'; exit 0; fi\nexit 97\n");
    chmodSync(fakeClaude, 0o755);
    const launch = () => spawnSync("bash", [join(SOURCE, "scripts/use-latest.sh"), "run", target,
      "--repo", "Examplecorp/examplelab", "--", "--model", "test"], {
      cwd: base, encoding: "utf8", env: { ...process.env, PATH: `${binary}:${process.env.PATH}`,
        PI_CODING_ORCHESTRATOR_REPO_URL: bare, PI_CODING_ORCHESTRATOR_HOME: cache,
        PI_CODING_AGENT_DIR: join(base, "agent") },
    });
    const launched = launch();
    assert.equal(launched.status, 0, launched.stderr);
    assert.match(launched.stdout, new RegExp(`CWD=${target}`));
    assert.match(launched.stdout, /REPO=Examplecorp\/examplelab/);
    // Sessions execute from an immutable snapshot of the resolved commit.
    const commit = run("git", ["rev-parse", "HEAD"], join(cache, "repo"));
    const runtime = join(cache, "runtimes", commit);
    assert.equal(readFileSync(join(runtime, ".pico-runtime"), "utf8").trim(), commit);
    for (const extension of ["model-router.ts", "workflow-commands.ts", "mission-controller.ts", "orchestration-lifecycle.ts"]) {
      assert.ok(existsSync(join(runtime, "extensions", extension)));
      assert.ok(launched.stdout.includes(join(runtime, "extensions", extension)));
    }
    for (const resource of ["tools/parent_orchestration.py", "tools/provenance.py",
      "schemas/run-record.schema.json", "routing/backend-compatibility.json",
      "prompts/orchestrate.md", "prompts/orchestrate-core.md"]) {
      assert.ok(existsSync(join(runtime, resource)));
    }
    const runs = join(cache, "repo/provenance/runs");
    mkdirSync(runs, { recursive: true });
    const generated = join(runs, "run-generated.json");
    // Runtime run records are expected to be append-only cache artifacts. The
    // fixture seeds a subset of the repository, so explicitly ignore generated
    // run-*.json files exactly as a real cached installation must.
    writeFileSync(join(cache, "repo/.git/info/exclude"), "provenance/runs/run-*.json\n");
    writeFileSync(generated, "{}\n");
    const cacheStatus = run("git", ["status", "--porcelain=v1", "--untracked-files=all"], join(cache, "repo"));
    assert.equal(cacheStatus, "", `generated provenance should be ignored; cache status was: ${cacheStatus}`);
    const relaunched = launch();
    assert.equal(relaunched.status, 0,
      `a generated parent record must not block cache updates\nstdout:\n${relaunched.stdout}\nstderr:\n${relaunched.stderr}`);
    assert.ok(existsSync(generated), "cache update must preserve the parent record");
    writeFileSync(join(cache, "repo/unrelated.txt"), "do not discard\n");
    const dirty = launch();
    assert.notEqual(dirty.status, 0);
    assert.match(dirty.stderr, /Refusing to update dirty orchestrator cache/);
    assert.deepEqual(readdirSync(target), []);
  } finally { rmSync(base, { recursive: true, force: true }); }
});
