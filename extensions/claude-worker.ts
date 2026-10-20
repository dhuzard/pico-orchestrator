/**
 * Headless Claude Code worker for routed Claude steps (the ClaudeWorker tool).
 *
 * Each call is its own `claude -p` subprocess whose working directory is the
 * step's bound worktree, so several mutating Claude workers run concurrently
 * without changing the Pi process directory. The lifecycle extension resolves
 * every input of a run (executable, cwd, model, effort, mode); the model never
 * chooses them.
 */
import { spawn } from "node:child_process";

export type ClaudeWorkerPlan = { executable: string; cwd: string; backendModel: string;
  effort: string; mode: "read" | "full" };
export type ClaudeWorkerUsage = { input_tokens: number | null; output_tokens: number | null;
  cached_tokens: number | null; cost_usd: number | null };
export type ClaudeWorkerOutcome = { ok: boolean; text: string; exitCode: number | null;
  error: "TIMEOUT" | "ABORTED" | "OUTPUT_LIMIT" | "SPAWN_FAILED" | "MALFORMED_OUTPUT" | "BACKEND_ERROR" | null;
  usage: ClaudeWorkerUsage; durationMs: number | null; elapsedMs: number; sessionId: string | null;
  permissionDenials: number; deniedCalls: string[] };

/** Built-in tools of a mutating step: edit and run local checks inside the worktree. */
export const MUTATING_TOOLS: readonly string[] = ["Read", "Edit", "Write", "Glob", "Grep", "Bash", "Agent"];
/** Built-in tools of a read-only step. */
export const READ_TOOLS: readonly string[] = ["Read", "Glob", "Grep"];
/**
 * Git and GitHub operations reserved for the parent. Deny rules win over the
 * allowed set; anything not listed here relies on the worker contract and on
 * the parent-checkout snapshot comparison.
 */
export const FORBIDDEN_TOOLS: readonly string[] = ["Bash(git push:*)", "Bash(gh:*)", "Bash(git worktree:*)",
  "Bash(git branch -d:*)", "Bash(git branch -D:*)", "Bash(git branch --delete:*)"];
export const DEFAULT_TIMEOUT_MS = 30 * 60_000;
const MAX_STDOUT = 8 * 1024 * 1024;
const MAX_STDERR = 64 * 1024;
const MAX_TEXT = 256 * 1024;
const KILL_GRACE_MS = 5_000;

/**
 * True when a path has a `.git` component. Claude Code protects every `.git`
 * directory: it denies Write/Edit there even under `--permission-mode
 * acceptEdits` with the tool allowed, and a headless worker cannot answer the
 * prompt. A worktree created as `<repo>/.git/<dir>/<task>` therefore leaves a
 * mutating worker unable to write anything (run-2dc716699308). Never loosen
 * the permission mode to work around it; bind a worktree outside `.git`.
 */
export function inGitDirectory(path: string): boolean {
  return path.split(/[\\/]+/).includes(".git");
}

/** Short `Tool path-or-command` descriptions of the calls Claude Code denied. */
export function describeDenials(denials: unknown): string[] {
  if (!Array.isArray(denials)) return [];
  return denials.slice(0, 10).map((item) => {
    const record = (item && typeof item === "object" ? item : {}) as Record<string, unknown>;
    const input = (record.tool_input && typeof record.tool_input === "object" ? record.tool_input : {}) as Record<string, unknown>;
    const target = [input.file_path, input.notebook_path, input.command, input.path].find((value) => typeof value === "string");
    const tool = typeof record.tool_name === "string" ? record.tool_name : "unknown tool";
    return (typeof target === "string" ? `${tool} ${target}` : tool).slice(0, 300);
  });
}

export function workerTimeoutMs(): number {
  const value = Number(process.env.PI_CLAUDE_WORKER_TIMEOUT_MS);
  return Number.isInteger(value) && value > 0 ? value : DEFAULT_TIMEOUT_MS;
}

/** Command-line arguments of one worker run; the prompt goes to stdin. */
export function claudeWorkerArgs(plan: ClaudeWorkerPlan): string[] {
  const tools = plan.mode === "full" ? MUTATING_TOOLS : READ_TOOLS;
  const contract = plan.mode === "full"
    ? `You are a pico-coding-orchestrator worker. Your working directory ${plan.cwd} is the git worktree bound to your step: edit only inside it. Never push, merge, create or remove worktrees or branches, or call GitHub; report what the parent must do instead.`
    : `You are a read-only pico-coding-orchestrator worker in ${plan.cwd}. Do not modify any file; report findings only.`;
  return ["-p", "--model", plan.backendModel, "--effort", plan.effort,
    "--permission-mode", plan.mode === "full" ? "acceptEdits" : "default",
    "--tools", tools.join(","), "--allowedTools", ...tools, "--disallowedTools", ...FORBIDDEN_TOOLS,
    "--append-system-prompt", contract, "--output-format", "json", "--no-session-persistence"];
}

function count(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

/** Parse the single `--output-format json` result object defensively. */
export function parseClaudeResult(stdout: string): { result: Record<string, unknown>; usage: ClaudeWorkerUsage } | null {
  let parsed: unknown;
  try { parsed = JSON.parse(stdout.trim()); } catch { return null; }
  // Older releases print an array of stream messages; the result is the last one.
  const result = Array.isArray(parsed) ? [...parsed].reverse().find((item) => item && typeof item === "object"
    && (item as Record<string, unknown>).type === "result") : parsed;
  if (!result || typeof result !== "object") return null;
  const record = result as Record<string, unknown>;
  const usage = (record.usage && typeof record.usage === "object" ? record.usage : {}) as Record<string, unknown>;
  const cost = record.total_cost_usd;
  return { result: record, usage: { input_tokens: count(usage.input_tokens), output_tokens: count(usage.output_tokens),
    cached_tokens: count(usage.cache_read_input_tokens),
    cost_usd: typeof cost === "number" && Number.isFinite(cost) && cost >= 0 ? cost : null } };
}

/** Run one worker to completion, honoring the abort signal, a timeout, and an output cap. */
export function runClaudeWorker(plan: ClaudeWorkerPlan, prompt: string, signal?: AbortSignal,
                                timeoutMs = workerTimeoutMs()): Promise<ClaudeWorkerOutcome> {
  const started = Date.now();
  const empty: ClaudeWorkerUsage = { input_tokens: null, output_tokens: null, cached_tokens: null, cost_usd: null };
  return new Promise((resolve) => {
    let stdout = "";
    let stderr = "";
    let failure: ClaudeWorkerOutcome["error"] = null;
    let settled = false;
    const child = spawn(plan.executable, claudeWorkerArgs(plan), { cwd: plan.cwd, shell: false,
      stdio: ["pipe", "pipe", "pipe"], env: process.env });
    const stop = (reason: NonNullable<ClaudeWorkerOutcome["error"]>) => {
      if (failure || settled) return;
      failure = reason;
      child.kill("SIGTERM");
      setTimeout(() => { if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL"); },
        KILL_GRACE_MS).unref();
    };
    const timer = setTimeout(() => stop("TIMEOUT"), timeoutMs);
    const onAbort = () => stop("ABORTED");
    if (signal?.aborted) onAbort();
    else signal?.addEventListener("abort", onAbort, { once: true });
    const finish = (exitCode: number | null) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      const elapsedMs = Date.now() - started;
      const base = { exitCode, elapsedMs, sessionId: null, permissionDenials: 0, deniedCalls: [] as string[] };
      if (failure) {
        const detail = failure === "TIMEOUT" ? `timed out after ${timeoutMs} ms`
          : failure === "ABORTED" ? "aborted" : failure === "OUTPUT_LIMIT" ? `exceeded ${MAX_STDOUT} bytes of output`
          : stderr.trim().slice(0, 2000) || "could not start the Claude executable";
        resolve({ ok: false, text: `Claude worker ${detail}.`, error: failure, usage: empty, durationMs: null, ...base });
        return;
      }
      const parsed = parseClaudeResult(stdout);
      if (!parsed) {
        resolve({ ok: false, text: stderr.trim().slice(0, 2000) || "Claude worker returned no JSON result.",
          error: "MALFORMED_OUTPUT", usage: empty, durationMs: null, ...base });
        return;
      }
      const { result, usage } = parsed;
      const text = typeof result.result === "string" ? result.result
        : typeof result.subtype === "string" ? `Claude worker ended with ${result.subtype}.` : "";
      const failed = result.is_error === true || exitCode !== 0;
      resolve({ ok: !failed, text: text.length > MAX_TEXT ? `${text.slice(0, MAX_TEXT)}\n[truncated]` : text,
        error: failed ? "BACKEND_ERROR" : null, usage, durationMs: count(result.duration_ms), exitCode, elapsedMs,
        sessionId: typeof result.session_id === "string" ? result.session_id : null,
        permissionDenials: Array.isArray(result.permission_denials) ? result.permission_denials.length : 0,
        deniedCalls: describeDenials(result.permission_denials) });
    };
    child.stdout.on("data", (part: Buffer) => {
      stdout += part.toString("utf8");
      if (stdout.length > MAX_STDOUT) { stdout = stdout.slice(0, MAX_STDOUT); stop("OUTPUT_LIMIT"); }
    });
    child.stderr.on("data", (part: Buffer) => { stderr = (stderr + part.toString("utf8")).slice(-MAX_STDERR); });
    child.on("error", (error) => {
      if (!failure) { failure = "SPAWN_FAILED"; stderr = `${error.name}: ${error.message}`; }
      finish(null);
    });
    child.on("close", (code) => finish(code));
    child.stdin.on("error", () => {}); // a child that exits early closes stdin; the close handler reports it
    child.stdin.end(prompt);
  });
}
