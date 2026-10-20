/**
 * mission-controller: thin Pi command layer for durable mission state.
 *
 * The deterministic state machine lives in tools/mission.py and
 * tools/mission_store.py. This extension does not implement routing, planning,
 * GitHub mutation, push, PR readiness, or merge behavior.
 */

import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const EXTENSION_DIR = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(EXTENSION_DIR, "..");
const MISSION_SCRIPT = join(REPO_ROOT, "tools", "mission.py");
const MAX_OUTPUT_BYTES = 256 * 1024;

function pythonExecutable(): string {
  return process.env.PI_MISSION_PYTHON ?? process.env.PI_ROUTER_PYTHON ?? "python3";
}

interface MissionCommandResult {
  ok: boolean;
  stdout: string;
  stderr: string;
  code: number | null;
}

function trimOutput(value: string): string {
  if (Buffer.byteLength(value, "utf-8") <= MAX_OUTPUT_BYTES) return value;
  return value.slice(0, MAX_OUTPUT_BYTES) + "\n[truncated]";
}

function runMissionCommand(args: string[], cwd: string, signal?: AbortSignal): Promise<MissionCommandResult> {
  return new Promise((resolve) => {
    const child = spawn(
      pythonExecutable(),
      [MISSION_SCRIPT, "--target", cwd, ...args],
      { cwd: REPO_ROOT, stdio: ["ignore", "pipe", "pipe"], signal },
    );
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk: Buffer) => { stdout = trimOutput(stdout + chunk.toString("utf-8")); });
    child.stderr.on("data", (chunk: Buffer) => { stderr = trimOutput(stderr + chunk.toString("utf-8")); });
    child.on("error", (error: Error) => {
      stderr = trimOutput(stderr + error.message);
    });
    child.on("close", (code: number | null) => {
      resolve({ ok: code === 0, stdout, stderr, code });
    });
  });
}

function renderResult(result: MissionCommandResult): string {
  if (result.ok) return result.stdout.trim() || "{}";
  const stderr = result.stderr.trim() || `mission command failed with exit code ${result.code ?? "unknown"}`;
  return `Mission command failed:\n${stderr}`;
}

async function showMissionResult(ctx: Parameters<Parameters<ExtensionAPI["registerCommand"]>[1]["handler"]>[1], result: MissionCommandResult) {
  ctx.ui.notify(result.ok ? "Mission command completed" : "Mission command failed", result.ok ? "info" : "error");
  ctx.ui.setEditorText(renderResult(result));
}

export default function missionController(pi: ExtensionAPI) {
  pi.registerCommand("deliver", {
    description: "Create a durable mission and queue bounded /orchestrate planning for the goal",
    handler: async (args, ctx) => {
      const goal = args.trim();
      if (!goal) {
        ctx.ui.notify("Usage: /deliver <engineering goal>", "error");
        return;
      }
      const result = await runMissionCommand(["create", "--goal", goal], ctx.cwd, ctx.signal);
      await showMissionResult(ctx, result);
      if (!result.ok) return;
      pi.sendUserMessage(
        `/orchestrate Plan the next dependency-ready wave for this delivery mission without broadening /orchestrate into a persistent controller. Mission goal: ${goal}`,
        { deliverAs: "followUp", expandPromptTemplates: true },
      );
    },
  });

  pi.registerCommand("deliver-status", {
    description: "Show one mission by id, or list all missions when no id is provided",
    handler: async (args, ctx) => {
      const id = args.trim();
      const result = await runMissionCommand(id ? ["show", id] : ["list"], ctx.cwd, ctx.signal);
      await showMissionResult(ctx, result);
    },
  });

  pi.registerCommand("deliver-link", {
    description: "Link an existing backlog item to a mission",
    handler: async (args, ctx) => {
      const [id, backlogItem, ...extra] = args.trim().split(/\s+/).filter(Boolean);
      if (!id || !backlogItem || extra.length > 0) {
        ctx.ui.notify("Usage: /deliver-link MISSION-001 ORCH-001", "error");
        return;
      }
      const result = await runMissionCommand(["add-backlog-item", id, backlogItem], ctx.cwd, ctx.signal);
      await showMissionResult(ctx, result);
    },
  });

  pi.registerCommand("deliver-pause", {
    description: "Pause a running mission",
    handler: async (args, ctx) => {
      const id = args.trim();
      if (!id) {
        ctx.ui.notify("Usage: /deliver-pause MISSION-001", "error");
        return;
      }
      const result = await runMissionCommand(["pause", id], ctx.cwd, ctx.signal);
      await showMissionResult(ctx, result);
    },
  });

  pi.registerCommand("deliver-resume", {
    description: "Resume a planned or paused mission",
    handler: async (args, ctx) => {
      const id = args.trim();
      if (!id) {
        ctx.ui.notify("Usage: /deliver-resume MISSION-001", "error");
        return;
      }
      const result = await runMissionCommand(["resume", id], ctx.cwd, ctx.signal);
      await showMissionResult(ctx, result);
    },
  });

  pi.registerCommand("deliver-cancel", {
    description: "Cancel a mission after explicit confirmation",
    handler: async (args, ctx) => {
      const id = args.trim();
      if (!id) {
        ctx.ui.notify("Usage: /deliver-cancel MISSION-001", "error");
        return;
      }
      if (!ctx.hasUI) {
        ctx.ui.notify("/deliver-cancel requires interactive confirmation", "error");
        return;
      }
      const confirmed = await ctx.ui.confirm("Cancel mission?", `${id} will become terminally cancelled. Backlog and GitHub state will not be changed.`);
      if (!confirmed) {
        ctx.ui.notify("Mission cancel aborted", "info");
        return;
      }
      const result = await runMissionCommand(["cancel", id], ctx.cwd, ctx.signal);
      await showMissionResult(ctx, result);
    },
  });
}
