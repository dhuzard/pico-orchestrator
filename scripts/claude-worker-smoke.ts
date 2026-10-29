/**
 * Real-backend smoke check for the ClaudeWorker permission model (makes one
 * paid Claude Code call, so it runs only with PICO_REAL_CLAUDE_SMOKE=1).
 *
 *   PICO_REAL_CLAUDE_SMOKE=1 node scripts/claude-worker-smoke.ts [claude-executable] [model]
 *
 * It creates a throwaway repository with a linked worktree at
 * `.pi/worktrees/s1`, checks that the real session_scope refuses a worktree
 * inside `.git` (WORKER_WORKSPACE_PROTECTED_PATH), then runs one mutating
 * worker with the exact argv of extensions/claude-worker.ts and requires that
 * it created a file in its worktree with no permission denials and left the
 * parent checkout untouched.
 */
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, realpathSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { runClaudeWorker } from "../extensions/claude-worker.ts";

if (process.env.PICO_REAL_CLAUDE_SMOKE !== "1") {
  console.log("SKIPPED: set PICO_REAL_CLAUDE_SMOKE=1 to make one real Claude Code call.");
  process.exit(0);
}
const executable = process.argv[2] ?? "claude";
const model = process.argv[3] ?? "haiku";
const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const base = realpathSync(mkdtempSync(join(tmpdir(), "pico-claude-smoke-")));
const repo = join(base, "repo");
const git = (...args: string[]) => execFileSync("git", ["-C", repo, ...args], { encoding: "utf8" }).trim();
let failed = false;
const check = (ok: boolean, label: string) => { console.log(`${ok ? "PASS" : "FAIL"} ${label}`); failed ||= !ok; };
try {
  execFileSync("git", ["init", "-q", "-b", "main", repo]);
  git("-c", "user.name=smoke", "-c", "user.email=smoke@example.com", "commit", "-q", "--allow-empty", "-m", "init");
  const worktree = join(repo, ".pi", "worktrees", "s1");
  const protectedPath = join(repo, ".git", "pico-worktrees", "s2");
  git("worktree", "add", "-q", "-b", "s1", worktree);
  git("worktree", "add", "-q", "-b", "s2", protectedPath);
  const scope = (path: string) => JSON.parse(execFileSync("python3", [join(root, "tools", "session_scope.py")],
    { input: JSON.stringify({ operation: "worktree", target_cwd: repo, path }), encoding: "utf8" }));
  check(scope(worktree).ok === true, "a worktree under .pi/worktrees can be bound");
  check(scope(protectedPath).blocker_code === "WORKER_WORKSPACE_PROTECTED_PATH", "a worktree inside .git is refused");
  const parentBefore = git("status", "--porcelain");
  const outcome = await runClaudeWorker({ executable, cwd: worktree, backendModel: model, effort: "low", mode: "full" },
    "Create a file named SMOKE.md in the current directory containing the single line: ok. Then reply done.",
    undefined, 300_000);
  check(outcome.ok, `worker finished (${outcome.error ?? "ok"})`);
  check(outcome.permissionDenials === 0, `no permission denials ${JSON.stringify(outcome.deniedCalls)}`);
  check(existsSync(join(worktree, "SMOKE.md")), "the worker wrote inside its bound worktree");
  check(git("status", "--porcelain") === parentBefore, "the parent checkout is unchanged");
  // The worker sandbox is on unless PI_ORCHESTRATOR_WORKER_SANDBOX=0 (#63); say which posture this call ran under.
  console.log(`sandbox=${JSON.stringify(outcome.sandbox)}`);
  console.log(`cost_usd=${outcome.usage.cost_usd ?? "unknown"}`);
} finally {
  rmSync(base, { recursive: true, force: true });
}
process.exit(failed ? 1 : 0);
