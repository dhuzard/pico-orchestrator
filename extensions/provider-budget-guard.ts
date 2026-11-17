/** Always injected by the budget gateway, including direct Pi runs. */
import { existsSync } from "node:fs";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function providerBudgetGuard(pi: ExtensionAPI) {
  pi.on("tool_call", (call, ctx) => {
    if ((call.toolName === "AskClaude" || call.toolName === "ClaudeWorker")
        && existsSync(join(ctx.cwd, ".pi", "claude-bridge.json"))) {
      return { block: true, reason: "PROVIDER_BUDGET_UNSUPPORTED: project Claude bridge configuration may override the budgeted executable" };
    }
    return undefined;
  });
}
