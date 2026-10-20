/** Observational parent context telemetry. Pi owns compaction decisions. */
export interface BudgetState {
  baseline: number | null;
  peakContext: number;
  peakWorking: number;
}

export function initialBudgetState(): BudgetState {
  return { baseline: null, peakContext: 0, peakWorking: 0 };
}

/** Count working tokens above the first observed context size, for provenance only. */
export function observeContext(state: BudgetState, tokens: number):
  { state: BudgetState; working: number } {
  const next = { ...state };
  if (next.baseline === null) next.baseline = tokens;
  const working = Math.max(0, tokens - next.baseline);
  next.peakContext = Math.max(next.peakContext, tokens);
  next.peakWorking = Math.max(next.peakWorking, working);
  return { state: next, working };
}
