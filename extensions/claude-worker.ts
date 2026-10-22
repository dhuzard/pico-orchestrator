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
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { delimiter, dirname, isAbsolute, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

export type ClaudeWorkerPlan = { executable: string; cwd: string; backendModel: string;
  effort: string; mode: "read" | "full";
  /** The run's sandbox posture and repository allowlist, resolved at run start; from the environment when absent. */
  sandbox?: WorkerSandboxConfig };
export type ClaudeWorkerUsage = { input_tokens: number | null; output_tokens: number | null;
  cached_tokens: number | null; cache_write_tokens: number | null; cost_usd: number | null };
export type ClaudeWorkerOutcome = { ok: boolean; text: string; exitCode: number | null;
  error: "TIMEOUT" | "ABORTED" | "OUTPUT_LIMIT" | "SPAWN_FAILED" | "MALFORMED_OUTPUT" | "BACKEND_ERROR"
    | "CONFIG_INVALID" | "SANDBOX_UNAVAILABLE" | null;
  usage: ClaudeWorkerUsage; durationMs: number | null; elapsedMs: number; sessionId: string | null;
  permissionDenials: number; deniedCalls: string[];
  /** Names (never values) of parent environment variables withheld from the worker. */
  envDropped: string[];
  /** The sandbox this worker ran under, or would have run under when it was not started; null on a config error. */
  sandbox: WorkerSandboxSummary | null };

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
 * comparison, and the OS sandbox around a mutating worker's Bash commands
 * (`workerSandboxSettings`, on by default).
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

/**
 * Worker sandbox (#63). A mutating Claude worker's Bash commands run in Claude
 * Code's own OS sandbox (bubblewrap and socat on Linux and WSL2, Seatbelt on
 * macOS) unless the user opts out explicitly with
 * PI_ORCHESTRATOR_WORKER_SANDBOX=0: unset or empty is the default (on), `1`
 * is on, `0` is the recorded opt-out, anything else is WORKER_CONFIG_INVALID.
 * Every settings key below was read from the settings schema bundled in Claude
 * Code 2.1.287 and from code.claude.com/docs/en/sandboxing and
 * /settings-reference; see docs/technical-reference.md, "Worker sandbox".
 */
export const WORKER_SANDBOX_VAR = "PI_ORCHESTRATOR_WORKER_SANDBOX";
export type WorkerSandboxPosture = { state: "enabled" | "opted_out"; source: "default" | "env" };
/** What a run's workers are sandboxed with: the posture plus the repository's opt-in allowlist. */
export type WorkerSandboxConfig = WorkerSandboxPosture & { allowedDomains: string[]; cacheDirectories: string[] };
/** What a worker result records about its sandbox (names and hosts only, never file contents). */
export type WorkerSandboxSummary = { state: "enabled" | "opted_out"; source: "default" | "env"; applied: boolean;
  allowed_domains: string[]; cache_directories: string[] };
/** Result of checking that the platform can run Claude Code's sandbox. */
export type SandboxSupport = { available: boolean; detail: string };
export type SandboxProbe = (env: Record<string, string>) => SandboxSupport;

/** Host allowlist entry: a DNS name or `*.` wildcard of at least two labels, optional `:port`; no bare `*`, no IP. */
export const SANDBOX_DOMAIN = /^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?(:[0-9]{1,5})?$/;
/** Cache directory entry: a relative path under the user cache directory, no `.`/`..` segments, no globs. */
export const SANDBOX_CACHE_DIRECTORY = /^[A-Za-z0-9_][A-Za-z0-9._-]*(\/[A-Za-z0-9_][A-Za-z0-9._-]*)*$/;
const MAX_ALLOWED_DOMAINS = 50;
const MAX_CACHE_DIRECTORIES = 20;
/** Directory name PICO's launcher uses under the cache, config and state roots. */
const PICO_DIR_NAME = "pi-coding-orchestrator";
/** This orchestrator checkout; its `provenance/` is the state directory when PI_ORCHESTRATOR_STATE_DIR is unset. */
const ORCHESTRATOR_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
/**
 * Credential locations relative to `$HOME` that sandboxed worker commands and
 * the Read tool must not read: GitHub CLI, SSH, Git credential stores, cloud
 * CLIs, container and package registries, and Claude Code's own stored login
 * (Claude Code reads it from its own process, outside the sandbox).
 */
export const SANDBOX_CREDENTIAL_PATHS: readonly string[] = [".config/gh", ".ssh", ".gnupg", ".netrc", ".git-credentials",
  ".config/git/credentials", ".aws", ".azure", ".config/gcloud", ".kube", ".docker/config.json", ".npmrc", ".pypirc",
  ".cargo/credentials.toml", ".config/hub", ".claude/.credentials.json"];

/** The posture from PI_ORCHESTRATOR_WORKER_SANDBOX; an unrecognized value throws WorkerConfigError. */
export function workerSandboxPosture(source: NodeJS.ProcessEnv = process.env): WorkerSandboxPosture {
  const value = (source[WORKER_SANDBOX_VAR] ?? "").trim();
  if (value === "") return { state: "enabled", source: "default" };
  if (value === "1") return { state: "enabled", source: "env" };
  if (value === "0") return { state: "opted_out", source: "env" };
  throw new WorkerConfigError(`${WORKER_SANDBOX_VAR} must be 0 (explicit opt-out) or 1; unset means on.`);
}

/** Whether worker Bash runs in Claude Code's sandbox (on unless PI_ORCHESTRATOR_WORKER_SANDBOX=0). */
export function workerSandboxEnabled(source: NodeJS.ProcessEnv = process.env): boolean {
  return workerSandboxPosture(source).state === "enabled";
}

/**
 * The run's sandbox configuration: the posture plus the repository's allowlist
 * (`worker_sandbox` in `.pi/autonomy.yaml`, already schema-validated by
 * tools/autonomy_policy.py). Entries are checked again here; an invalid one throws.
 */
export function workerSandboxConfig(policy: { allowed_domains?: unknown; cache_directories?: unknown } | null,
                                    source: NodeJS.ProcessEnv = process.env): WorkerSandboxConfig {
  const posture = workerSandboxPosture(source);
  const list = (value: unknown, pattern: RegExp, max: number, label: string): string[] => {
    if (value === undefined || value === null) return [];
    if (!Array.isArray(value) || value.length > max) {
      throw new WorkerConfigError(`worker_sandbox ${label} must be a list of at most ${max} entries.`);
    }
    for (const entry of value) {
      if (typeof entry !== "string" || !pattern.test(entry)) {
        throw new WorkerConfigError(`worker_sandbox ${label} entry ${JSON.stringify(String(entry).slice(0, 80))} is invalid.`);
      }
    }
    return [...new Set(value as string[])];
  };
  const cacheDirectories = list(policy?.cache_directories, SANDBOX_CACHE_DIRECTORY, MAX_CACHE_DIRECTORIES, "cache_directories");
  if (cacheDirectories.some((entry) => entry === PICO_DIR_NAME || entry.startsWith(`${PICO_DIR_NAME}/`))) {
    throw new WorkerConfigError(`worker_sandbox cache_directories may not include PICO's own ${PICO_DIR_NAME} cache.`);
  }
  return { ...posture, cacheDirectories,
    allowedDomains: list(policy?.allowed_domains, SANDBOX_DOMAIN, MAX_ALLOWED_DOMAINS, "network.allowed_domains") };
}

/**
 * Whether Claude Code's sandbox can start here, from what its documentation
 * names as requirements: Seatbelt (`sandbox-exec`) on macOS, `bwrap` and
 * `socat` on the worker's PATH on Linux (WSL2 included). Other platforms,
 * native Windows among them, have no sandbox. Claude Code's own
 * `failIfUnavailable` check stays the final word; this one only lets PICO
 * fail closed before starting a process, with a stable code.
 */
export function detectWorkerSandbox(env: Record<string, string>, platform: NodeJS.Platform = process.platform,
                                    exists: (path: string) => boolean = existsSync): SandboxSupport {
  if (platform === "darwin") {
    return exists("/usr/bin/sandbox-exec") ? { available: true, detail: "Seatbelt (sandbox-exec)" }
      : { available: false, detail: "macOS without /usr/bin/sandbox-exec" };
  }
  if (platform !== "linux") return { available: false, detail: `no Claude Code sandbox on platform ${platform}` };
  const onPath = (name: string) => (env.PATH ?? "").split(delimiter)
    .some((dir) => dir !== "" && isAbsolute(dir) && exists(join(dir, name)));
  const missing = ["bwrap", "socat"].filter((name) => !onPath(name));
  return missing.length === 0 ? { available: true, detail: "bubblewrap and socat" }
    : { available: false, detail: `${missing.join(" and ")} not found on the worker PATH (install bubblewrap and socat)` };
}

function absoluteOrNull(value: string | undefined): string | null {
  return value && isAbsolute(value) ? resolve(value) : null;
}

/** Paths a worker must not read: PICO state directories (provenance, leases) and credential locations. */
export function sandboxDeniedReads(parentEnv: NodeJS.ProcessEnv, home: string): string[] {
  const xdgState = absoluteOrNull(parentEnv.XDG_STATE_HOME) ?? join(home, ".local", "state");
  const xdgConfig = absoluteOrNull(parentEnv.XDG_CONFIG_HOME);
  const state = [absoluteOrNull(parentEnv.PI_ORCHESTRATOR_STATE_DIR) ?? join(ORCHESTRATOR_ROOT, "provenance"),
    absoluteOrNull(parentEnv.PI_CODING_ORCHESTRATOR_STATE_DIR), join(xdgState, PICO_DIR_NAME)];
  const credentials = [...SANDBOX_CREDENTIAL_PATHS.map((path) => join(home, path)),
    xdgConfig ? join(xdgConfig, "gh") : null, absoluteOrNull(parentEnv.GH_CONFIG_DIR)];
  return [...new Set([...state, ...credentials].filter((path): path is string => path !== null))];
}

/** PICO's launcher cache (it holds the code PICO runs) and user configuration: never writable by a worker. */
export function sandboxProtectedWrites(parentEnv: NodeJS.ProcessEnv, home: string): string[] {
  const cache = absoluteOrNull(parentEnv.XDG_CACHE_HOME) ?? join(home, ".cache");
  const config = absoluteOrNull(parentEnv.XDG_CONFIG_HOME) ?? join(home, ".config");
  return [...new Set([absoluteOrNull(parentEnv.PI_CODING_ORCHESTRATOR_HOME) ?? join(cache, PICO_DIR_NAME),
    join(config, PICO_DIR_NAME)])];
}

function within(path: string, root: string): boolean {
  return path === root || path.startsWith(root.endsWith(sep) ? root : root + sep);
}

/**
 * The `--settings` object of one worker. Always: `permissions.deny` Read rules
 * for the PICO state directory and credential locations (they bind Claude's
 * Read, Edit and Write tools, which the OS sandbox does not cover). For a
 * mutating worker with the sandbox on, also `sandbox`: enabled, exit at
 * startup when it cannot start, no unsandboxed retry (which also makes Claude
 * Code ignore a repository's own `.claude/settings.json` widening entries),
 * the same reads denied at the OS level, writes beyond the working directory
 * (the bound worktree; Claude Code adds the shared `.git` of a linked worktree,
 * except its hooks and config) only to the declared cache directories, and
 * network only to the allowlisted hosts, refused deterministically
 * (`strictAllowlist`) instead of prompting. Throws WorkerConfigError when a
 * denied path or PICO directory would contain the worktree, or overlaps a cache
 * directory, since the boundary could then not be expressed.
 */
export function workerSandboxSettings(input: { cwd: string; mode: "read" | "full"; config: WorkerSandboxConfig;
                                               home: string; parentEnv?: NodeJS.ProcessEnv }): Record<string, unknown> {
  const parentEnv = input.parentEnv ?? process.env;
  const cwd = resolve(input.cwd);
  const deniedReads = sandboxDeniedReads(parentEnv, input.home);
  const protectedWrites = sandboxProtectedWrites(parentEnv, input.home);
  const guarded = [...deniedReads, ...protectedWrites];
  const inside = guarded.find((path) => within(cwd, path));
  if (inside) throw new WorkerConfigError(`the worker directory ${cwd} is inside ${inside}, which the worker sandbox protects.`);
  const cacheRoot = absoluteOrNull(parentEnv.XDG_CACHE_HOME) ?? join(input.home, ".cache");
  const caches = input.config.cacheDirectories.map((entry) => join(cacheRoot, entry));
  for (const cache of caches) {
    const clash = guarded.find((path) => within(cache, path) || within(path, cache));
    if (clash) throw new WorkerConfigError(`worker_sandbox cache directory ${cache} overlaps ${clash}, which the worker sandbox protects.`);
  }
  // Permission rules take `//` for an absolute path; sandbox paths take a plain absolute path.
  const deny = deniedReads.flatMap((path) => [`Read(/${path})`, `Read(/${path}/**)`]);
  const settings: Record<string, unknown> = { permissions: { deny } };
  if (input.mode === "full" && input.config.state === "enabled") {
    settings.sandbox = { enabled: true, failIfUnavailable: true, allowUnsandboxedCommands: false,
      filesystem: { ...(caches.length ? { allowWrite: caches } : {}), denyRead: deniedReads, denyWrite: protectedWrites },
      network: { allowedDomains: input.config.allowedDomains, strictAllowlist: true } };
  }
  return settings;
}

/** Claude Code's own startup refusal when `failIfUnavailable` is set and its sandbox cannot start. */
export function sandboxUnavailableText(text: string): boolean {
  return /sandbox/i.test(text) && /failIfUnavailable/.test(text);
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

/**
 * Command-line arguments of one worker run; the prompt goes to stdin. `settings`
 * (from `workerSandboxSettings`) is passed as one `--settings` JSON argument.
 */
export function claudeWorkerArgs(plan: ClaudeWorkerPlan, settings?: Record<string, unknown>): string[] {
  const tools = plan.mode === "full" ? MUTATING_TOOLS : READ_TOOLS;
  const contract = plan.mode === "full"
    ? `You are a pico-coding-orchestrator worker. Your working directory ${plan.cwd} is the git worktree bound to your step: edit only inside it. Never push, merge, create or remove worktrees or branches, or call GitHub; report what the parent must do instead.`
    : `You are a read-only pico-coding-orchestrator worker in ${plan.cwd}. Do not modify any file; report findings only.`;
  return ["-p", "--model", plan.backendModel, "--effort", plan.effort,
    "--permission-mode", plan.mode === "full" ? "acceptEdits" : "default",
    "--tools", tools.join(","), "--allowedTools", ...tools, "--disallowedTools", ...FORBIDDEN_TOOLS,
    "--append-system-prompt", contract, "--output-format", "json", "--no-session-persistence",
    ...(settings ? ["--settings", JSON.stringify(settings)] : [])];
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
                                timeoutMs = workerTimeoutMs(),
                                options: { probe?: SandboxProbe; parentEnv?: NodeJS.ProcessEnv } = {}): Promise<ClaudeWorkerOutcome> {
  const started = Date.now();
  const empty: ClaudeWorkerUsage = { input_tokens: null, output_tokens: null, cached_tokens: null, cache_write_tokens: null, cost_usd: null };
  const parentEnv = options.parentEnv ?? process.env;
  const notStarted = (error: "CONFIG_INVALID" | "SANDBOX_UNAVAILABLE", text: string, dropped: string[],
                      sandbox: WorkerSandboxSummary | null): Promise<ClaudeWorkerOutcome> =>
    Promise.resolve({ ok: false, text: `Claude worker not started: ${text}`, error,
      usage: empty, durationMs: null, exitCode: null, elapsedMs: Date.now() - started, sessionId: null,
      permissionDenials: 0, deniedCalls: [], envDropped: dropped, sandbox });
  let prepared: { env: Record<string, string>; dropped: string[]; settings: Record<string, unknown>;
    summary: WorkerSandboxSummary };
  try {
    const config = plan.sandbox ?? workerSandboxConfig(null, parentEnv);
    const { env, dropped } = buildWorkerEnv(parentEnv);
    const settings = workerSandboxSettings({ cwd: plan.cwd, mode: plan.mode, config, parentEnv,
      home: env.HOME && isAbsolute(env.HOME) ? env.HOME : homedir() });
    prepared = { env, dropped, settings, summary: { state: config.state, source: config.source,
      applied: "sandbox" in settings, allowed_domains: config.allowedDomains, cache_directories: config.cacheDirectories } };
  } catch (error) {
    if (!(error instanceof WorkerConfigError)) throw error;
    return notStarted("CONFIG_INVALID", error.message, [], null);
  }
  const { env, dropped, settings, summary } = prepared;
  if (summary.applied) {
    // A mutating worker that should be sandboxed and cannot be never starts unconfined.
    const support = (options.probe ?? detectWorkerSandbox)(env);
    if (!support.available) {
      return notStarted("SANDBOX_UNAVAILABLE", `the worker sandbox is unavailable: ${support.detail}. ` +
        `Install its prerequisites, or opt out explicitly with ${WORKER_SANDBOX_VAR}=0 (recorded on the run).`, dropped, summary);
    }
  }
  return new Promise((resolve) => {
    let stdout = "";
    let stderr = "";
    let failure: ClaudeWorkerOutcome["error"] = null;
    let settled = false;
    const child = spawn(plan.executable, claudeWorkerArgs(plan, settings), { cwd: plan.cwd, shell: false,
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
        envDropped: dropped, sandbox: summary };
      // Claude Code's own failIfUnavailable refusal: the sandbox could not start, so nothing ran.
      const refusedSandbox = (text: string) => summary.applied && exitCode !== 0 && sandboxUnavailableText(text);
      if (failure) {
        const detail = failure === "TIMEOUT" ? `timed out after ${timeoutMs} ms`
          : failure === "ABORTED" ? "aborted" : failure === "OUTPUT_LIMIT" ? `exceeded ${MAX_STDOUT} bytes of output`
          : stderr.trim().slice(0, 2000) || "could not start the Claude executable";
        resolve({ ok: false, text: `Claude worker ${detail}.`, error: failure, usage: empty, durationMs: null, ...base });
        return;
      }
      const parsed = parseClaudeResult(stdout);
      if (!parsed) {
        const text = stderr.trim().slice(0, 2000) || "Claude worker returned no JSON result.";
        resolve({ ok: false, text, error: refusedSandbox(`${stderr}\n${stdout}`) ? "SANDBOX_UNAVAILABLE" : "MALFORMED_OUTPUT",
          usage: empty, durationMs: null, ...base });
        return;
      }
      const { result, usage } = parsed;
      const text = typeof result.result === "string" ? result.result
        : typeof result.subtype === "string" ? `Claude worker ended with ${result.subtype}.` : "";
      const failed = result.is_error === true || exitCode !== 0;
      resolve({ ok: !failed, text: text.length > MAX_TEXT ? `${text.slice(0, MAX_TEXT)}\n[truncated]` : text,
        error: !failed ? null : refusedSandbox(`${text}\n${stderr}`) ? "SANDBOX_UNAVAILABLE" : "BACKEND_ERROR", usage, durationMs: count(result.duration_ms), exitCode, elapsedMs,
        sessionId: typeof result.session_id === "string" ? result.session_id : null,
        permissionDenials: Array.isArray(result.permission_denials) ? result.permission_denials.length : 0,
        deniedCalls: describeDenials(result.permission_denials), envDropped: dropped, sandbox: summary });
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
