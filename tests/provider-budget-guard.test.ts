import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import providerBudgetGuard from "../extensions/provider-budget-guard.ts";

test("gateway guard blocks dynamically created bridge overrides, including outside a parent run", async () => {
  const directory = mkdtempSync(join(tmpdir(), "pico-budget-guard-"));
  let handler: ((call: any, ctx: any) => any) | undefined;
  providerBudgetGuard({ on(_event: string, fn: typeof handler) { handler = fn; } } as ExtensionAPI);
  try {
    const call = { toolName: "AskClaude", input: { isolated: true } };
    assert.equal(await handler!(call, { cwd: directory }), undefined);
    mkdirSync(join(directory, ".pi"));
    writeFileSync(join(directory, ".pi", "claude-bridge.json"), '{"provider":{"pathToClaudeCodeExecutable":"/unbudgeted/claude"}}');
    const result = await handler!(call, { cwd: directory });
    assert.equal(result.block, true);
    assert.match(result.reason, /PROVIDER_BUDGET_UNSUPPORTED/);
    assert.equal(await handler!({ toolName: "read", input: {} }, { cwd: directory }), undefined);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
