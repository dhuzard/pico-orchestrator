/** User-facing workflow commands for pi-coding-orchestrator. */
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const INIT = join(ROOT, "tools", "init_project.py");
const ORCHESTRATE_CORE_PROMPT = join(ROOT, "prompts", "orchestrate-core.md");

export const WORKFLOW_COMMANDS = [
  "initialize", "lookup", "orchestrate", "audit", "audit-backend", "audit-frontend",
  "quick-audit", "plan-implementation", "update-agents", "orchestrate-auto",
] as const;

const wrappers: Record<string, string> = {
  orchestrate: "",
  audit: "Audit this repository comprehensively for correctness, security, reliability, maintainability, and test gaps. ",
  "audit-backend": "Audit the backend for correctness, security, data integrity, APIs, concurrency, persistence, and tests. ",
  "audit-frontend": "Audit the frontend/UI for correctness, accessibility, UX regressions, state management, performance, and tests. ",
  "quick-audit": "Run a bounded, high-signal audit. Prioritize reproducible MUST-fix correctness and security defects. ",
  "plan-implementation": "Produce an implementation-ready plan. Inspect the repository first; identify files, dependencies, tests, risks, and sequencing. Do not implement unless explicitly asked. ",
  "update-agents": "Inspect the repository and improve AGENTS.md conservatively. Preserve valid project-specific instructions; add only evidenced commands, architecture, constraints, and validation guidance. ",
  "orchestrate-auto": "Operate autonomously within the repository's configured authority. Complete the objective through the shared orchestration protocol, stopping at configured approval boundaries. ",
};

/** Commands whose objective is optional; all others require one. */
export const DEFAULT_OBJECTIVES: Readonly<Record<string, string>> = {
  audit: "Audit this repository.",
  "audit-backend": "Audit this repository's backend.",
  "audit-frontend": "Audit this repository's frontend/UI.",
  "quick-audit": "Run a quick audit of this repository.",
  "update-agents": "Update this repository's AGENTS.md.",
};

export type WorkflowPrompt =
  | { ok: true; prompt: string }
  | { ok: false; reason: "objective_required" | "prompt_unavailable"; message: string };

/** Build the /orchestrate prompt for a workflow command without throwing. */
export function buildWorkflowPrompt(command: string, args: string | undefined, promptPath = ORCHESTRATE_CORE_PROMPT): WorkflowPrompt {
  const objective = (typeof args === "string" ? args : "").trim() || DEFAULT_OBJECTIVES[command] || "";
  if (!objective) {
    return { ok: false, reason: "objective_required", message: `Usage: /${command} <objective>` };
  }
  let template: string;
  try {
    template = readFileSync(promptPath, "utf8").replace(/^---[\s\S]*?---\s*/, "");
  } catch (error) {
    return { ok: false, reason: "prompt_unavailable",
      message: `/${command} unavailable: orchestration prompt not readable at ${promptPath} (${(error as Error).message}). ` +
        "Launch through scripts/use-latest.sh or scripts/run-local.sh." };
  }
  const request = `${wrappers[command] ?? ""}${objective}`;
  // A replacer function keeps `$&`, `$'`, etc. in user objectives literal.
  const prompt = template.includes("$ARGUMENTS")
    ? template.replace("$ARGUMENTS", () => request)
    : `${template.trimEnd()}\n\n${request}\n`;
  return { ok: true, prompt };
}

function runInit(cwd: string, signal?: AbortSignal): Promise<{code:number|null; stdout:string; stderr:string}> {
  return new Promise((resolve) => {
    const child = spawn(process.env.PI_ROUTER_PYTHON ?? "python3", [INIT, cwd], {cwd: ROOT, signal, stdio:["ignore","pipe","pipe"]});
    let stdout="", stderr="";
    child.stdout.on("data", (b:Buffer)=>stdout+=b.toString("utf8"));
    child.stderr.on("data", (b:Buffer)=>stderr+=b.toString("utf8"));
    child.on("error", (e:Error)=>{stderr+=e.message;});
    child.on("close", code=>resolve({code,stdout,stderr}));
  });
}

export default function workflowCommands(pi: ExtensionAPI) {
  pi.registerCommand("lookup", {
    description: "Answer a bounded read-only repository question directly in Pi",
    handler: async (args, ctx) => {
      const objective = args.trim();
      if (!objective) { ctx.ui.notify("Usage: /lookup <repository question>", "error"); return; }
      pi.sendUserMessage(`Answer this bounded repository question directly. Read only the relevant files. Do not edit files, start orchestration, route tasks, or spawn agents. Cite the file paths and lines that support the answer. If the request requires changes or a broad audit, explain that /orchestrate is appropriate.\n\n<question>\n${objective}\n</question>`,
        { deliverAs: "followUp", expandPromptTemplates: false });
    },
  });
  pi.registerCommand("initialize", {
    description: "Conservatively initialize this repository for pi-coding-orchestrator",
    handler: async (_args, ctx) => {
      const result = await runInit(ctx.cwd, ctx.signal);
      if (result.code !== 0) {
        ctx.ui.notify("Repository initialization failed", "error");
        ctx.ui.setEditorText(result.stderr.trim() || "Initialization failed");
        return;
      }
      ctx.ui.notify("Repository initialized; existing files were preserved", "info");
      ctx.ui.setEditorText(result.stdout.trim());
    },
  });

  for (const command of WORKFLOW_COMMANDS.filter(c => c !== "initialize" && c !== "lookup")) {
    pi.registerCommand(command, {
      description: command === "orchestrate"
        ? "Run the shared deterministic orchestration protocol"
        : `Run ${command} through the shared /orchestrate protocol` +
          (DEFAULT_OBJECTIVES[command] ? " (objective optional)" : ""),
      handler: async (args, ctx) => {
        // Never throw into Pi's extension error path: report failures to the user.
        try {
          const built = buildWorkflowPrompt(command, args);
          if (!built.ok) {
            ctx.ui.notify(built.message, "error");
            return;
          }
          pi.sendUserMessage(built.prompt, {deliverAs:"followUp", expandPromptTemplates:false});
        } catch (error) {
          ctx.ui.notify(`/${command} failed: ${(error as Error).message}`, "error");
        }
      },
    });
  }
}
