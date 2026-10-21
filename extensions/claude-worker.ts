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
  cached_tokens: number | null; cache_write_tokens: number | null; cost_usd: number | null };
export type ClaudeWorkerOutcome = { ok: boolean; text: string; exitCode: number | null;
  error: "TIMEOUT" | "ABORTED" | "OUTPUT_LIMIT" | "SPAWN_FAILED" | "MALFORMED_OUTPUT" | "BACKEND_ERROR"
    | "CONFIG_INVALID" | null;
  usage: ClaudeWorkerUsage; durationMs: number | null; elapsedMs: number; sessionId: string | null;
  permissionDenials: number; deniedCalls: string[];
  /** Names (never values) of parent environment variables withheld from the worker. */
  envDropped: string[] };

/** Built-in tools of a mutating step: edit and run local checks inside the worktree. */
export const MUTATING_TOOLS: readonly string[] = ["Read", "Edit", "Write", "Glob", "Grep", "Bash", "Agent"];
/** Built-in tools of a read-only step. */
export const READ_TOOLS: readonly string[] = ["Read", "Glob", "Grep"];
/**
 * Git and GitHub operations reserved for the parent, as Claude Code deny rules
 * (deny wins over the allowed set). This list is best-effort defense in depth,
 * not a boundary: Bash rules match a command by prefix, so a mutating worker
 * with Bash can still reach the same effect through scripts, other
 * interpreters or the network. The controls that hold are the worker
 * environment allowlist (`buildWorkerEnv`: no GitHub token, SSH agent or Git
 * credential prompt reaches the worker), the parent-checkout snapshot
 * comparison, and an OS sandbox around the worker, which we recommend
 * (`PI_ORCHESTRATOR_WORKER_SANDBOX=1`, a container, or bwrap).
 * A worker's cwd is its bound worktree, so it never needs to point git at
 * another repository (`-C`, `--git-dir`, `--work-tree`), override its
 * configuration on the command line (`-c`, for example an alias that pushes),
 * or add or redirect remotes.
 */
export const FORBIDDEN_TOOLS: readonly string[] = ["Bash(git push:*)", "Bash(gh:*)", "Bash(git worktree:*)",
  "Bash(git branch -d:*)", "Bash(git branch -D:*)", "Bash(git branch --delete:*)",
  "Bash(git -C:*)", "Bash(git -c:*)", "Bash(git --git-dir:*)", "Bash(git --work-tree:*)",
  "Bash(git --exec-path:*)", "Bash(git remote add:*)", "Bash(git remote set-url:*)",
  "Bash(env git:*)", "Bash(command git:*)", "Bash(exec git:*)",
  "Bash(env gh:*)", "Bash(command gh:*)", "Bash(exec gh:*)"];

/**
 * Exact environment variable names a worker receives: what Claude Code and the
 * repository's local checks need to run (process identity, locale, temp and
 * XDG directories, CA trust, proxies). Claude Code authentication reaches the
 * worker through `HOME` (the stored login) or the `ANTHROPIC_*` / `CLAUDE_*`
 * prefixes. PICO verifies Claude authentication only with `claude auth status`
 * (tools/backend_preflight.py) and documents no third-party provider mode, so
 * AWS and Google Cloud credentials are not forwarded by default.
 */
export const WORKER_ENV_NAMES: readonly string[] = ["PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE",
  "TERM", "COLORTERM", "TZ", "TMPDIR", "TMP", "TEMP", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
  "XDG_STATE_HOME", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY",
  "NO_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "no_proxy", "all_proxy"];
/** Name prefixes a worker receives: locale categories and Anthropic / Claude Code configuration. */
export const WORKER_ENV_PREFIXES: readonly string[] = ["LC_", "ANTHROPIC_", "CLAUDE_"];
/**
 * Credentials that let a process act on GitHub, a Git remote or a package
 * registry as the user. No built-in rule forwards them; only naming one in
 * PI_ORCHESTRATOR_WORKER_ENV_ALLOW does, which is then the user's explicit choice.
 */
export const WORKER_ENV_CREDENTIALS: readonly string[] = ["GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
  "GITHUB_ENTERPRISE_TOKEN", "GIT_ASKPASS", "SSH_ASKPASS", "SSH_AUTH_SOCK", "NPM_TOKEN", "NODE_AUTH_TOKEN"];
/** Comma-separated extra variable names to forward to workers. */
export const WORKER_ENV_ALLOW_VAR = "PI_ORCHESTRATOR_WORKER_ENV_ALLOW";
/** `1` runs worker Bash commands in Claude Code's sandbox and refuses to start without one. */
export const WORKER_SANDBOX_VAR = "PI_ORCHESTRATOR_WORKER_SANDBOX";
/**
 * Claude Code settings (passed with `--settings`) that sandbox Bash and make
 * Claude Code exit at startup when the sandbox cannot start, instead of
 * running commands unconfined.
 */
export const WORKER_SANDBOX_SETTINGS = JSON.stringify({ sandbox: { enabled: true, failIfUnavailable: true } });
const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** Invalid worker configuration; the worker is not started. */
export class WorkerConfigError extends Error {}

/**
 * Environment of one worker: allowlisted names from `source`, plus the exact
 * names listed in PI_ORCHESTRATOR_WORKER_ENV_ALLOW. Everything else (GitHub
 * tokens, SSH agent, Git overrides such as GIT_DIR, cloud and registry
 * credentials) is withheld; `dropped` lists the withheld names, never their
 * values: known credentials first (so a truncated record still names them),
 * then the rest sorted. An invalid name in the extension list throws
 * WorkerConfigError.
 */
export function buildWorkerEnv(source: NodeJS.ProcessEnv = process.env):
    { env: Record<string, string>; dropped: string[] } {
  const extra = new Set<string>();
  for (const raw of (source[WORKER_ENV_ALLOW_VAR] ?? "").split(",")) {
    const name = raw.trim();
    if (!name) continue;
    if (!ENV_NAME.test(name)) {
      throw new WorkerConfigError(`${WORKER_ENV_ALLOW_VAR} entry ${JSON.stringify(name.slice(0, 80))} is not a variable name.`);
    }
    extra.add(name);
  }
  const env: Record<string, string> = {};
  const dropped: string[] = [];
  for (const [name, value] of Object.entries(source)) {
    if (value === undefined) continue;
    const builtIn = !WORKER_ENV_CREDENTIALS.includes(name)
      && (WORKER_ENV_NAMES.includes(name) || WORKER_ENV_PREFIXES.some((prefix) => name.startsWith(prefix)));
    if (builtIn || extra.has(name)) env[name] = value;
    else dropped.push(name);
  }
  const credential = (name: string) => WORKER_ENV_CREDENTIALS.includes(name);
  dropped.sort((a, b) => Number(credential(b)) - Number(credential(a)) || (a < b ? -1 : a > b ? 1 : 0));
  return { env, dropped };
}

/** Whether worker Bash runs in Claude Code's sandbox: unset, empty or `0` is off, `1` is on, anything else throws. */
export function workerSandboxEnabled(source: NodeJS.ProcessEnv = process.env): boolean {
  const value = (source[WORKER_SANDBOX_VAR] ?? "").trim();
  if (value === "" || value === "0") return false;
  if (value === "1") return true;
  throw new WorkerConfigError(`${WORKER_SANDBOX_VAR} must be 0 or 1.`);
}
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
export function claudeWorkerArgs(plan: ClaudeWorkerPlan, sandbox = false): string[] {
  const tools = plan.mode === "full" ? MUTATING_TOOLS : READ_TOOLS;
  const contract = plan.mode === "full"
    ? `You are a pico-coding-orchestrator worker. Your working directory ${plan.cwd} is the git worktree bound to your step: edit only inside it. Never push, merge, create or remove worktrees or branches, or call GitHub; report what the parent must do instead.`
    : `You are a read-only pico-coding-orchestrator worker in ${plan.cwd}. Do not modify any file; report findings only.`;
  return ["-p", "--model", plan.backendModel, "--effort", plan.effort,
    "--permission-mode", plan.mode === "full" ? "acceptEdits" : "default",
    "--tools", tools.join(","), "--allowedTools", ...tools, "--disallowedTools", ...FORBIDDEN_TOOLS,
    "--append-system-prompt", contract, "--output-format", "json", "--no-session-persistence",
    ...(sandbox ? ["--settings", WORKER_SANDBOX_SETTINGS] : [])];
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
    // Cache reads are cheap and never budgeted; cache writes (cache_creation) are billed input.
    cached_tokens: count(usage.cache_read_input_tokens), cache_write_tokens: count(usage.cache_creation_input_tokens),
    cost_usd: typeof cost === "number" && Number.isFinite(cost) && cost >= 0 ? cost : null } };
}

/** Run one worker to completion, honoring the abort signal, a timeout, and an output cap. */
export function runClaudeWorker(plan: ClaudeWorkerPlan, prompt: string, signal?: AbortSignal,
                                timeoutMs = workerTimeoutMs()): Promise<ClaudeWorkerOutcome> {
  const started = Date.now();
  const empty: ClaudeWorkerUsage = { input_tokens: null, output_tokens: null, cached_tokens: null, cache_write_tokens: null, cost_usd: null };
  let prepared: { env: Record<string, string>; dropped: string[]; sandbox: boolean };
  try {
    prepared = { ...buildWorkerEnv(), sandbox: workerSandboxEnabled() };
  } catch (error) {
    if (!(error instanceof WorkerConfigError)) throw error;
    return Promise.resolve({ ok: false, text: `Claude worker not started: ${error.message}`, error: "CONFIG_INVALID",
      usage: empty, durationMs: null, exitCode: null, elapsedMs: Date.now() - started, sessionId: null,
      permissionDenials: 0, deniedCalls: [], envDropped: [] });
  }
  const { env, dropped, sandbox } = prepared;
  return new Promise((resolve) => {
    let stdout = "";
    let stderr = "";
    let failure: ClaudeWorkerOutcome["error"] = null;
    let settled = false;
    const child = spawn(plan.executable, claudeWorkerArgs(plan, sandbox), { cwd: plan.cwd, shell: false,
      stdio: ["pipe", "pipe", "pipe"], env });
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
      const base = { exitCode, elapsedMs, sessionId: null, permissionDenials: 0, deniedCalls: [] as string[],
        envDropped: dropped };
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
        deniedCalls: describeDenials(result.permission_denials), envDropped: dropped });
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
