import assert from "node:assert/strict";
import { test } from "node:test";
import { initialBudgetState, observeContext } from "../extensions/context-budget.ts";

test("working-token telemetry tracks peaks without a compaction trigger or hard gate", () => {
  let state = initialBudgetState();
  for (const tokens of [10000, 34000, 70000, 22000, 90000]) {
    const observed = observeContext(state, tokens);
    assert.equal(observed.working, Math.max(0, tokens - 10000));
    assert.equal("compact" in observed, false);
    state = observed.state;
  }
  assert.deepEqual(state, { baseline: 10000, peakContext: 90000, peakWorking: 80000 });
});
