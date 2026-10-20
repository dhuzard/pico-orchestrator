/** Translate policy model IDs only at the Claude Code tool boundary. */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const CONFIG = join(ROOT, "routing", "backend-compatibility.json");
const NATIVE_ID = /^[A-Za-z0-9][A-Za-z0-9-]*$/;

export type BackendModelResult =
  | { ok: true; canonical_model: string; backend_model: string }
  | { ok: false; blocker_code: "MODEL_IDENTIFIER_UNSUPPORTED" | "MODEL_BACKEND_CONFIG_INVALID" };

export function resolveClaudeBackendModel(canonicalModel: string, configPath = CONFIG): BackendModelResult {
  let config: unknown;
  try { config = JSON.parse(readFileSync(configPath, "utf8")); }
  catch { return { ok: false, blocker_code: "MODEL_BACKEND_CONFIG_INVALID" }; }
  if (!config || typeof config !== "object" || !("claude_code_model_ids" in config)) {
    return { ok: false, blocker_code: "MODEL_BACKEND_CONFIG_INVALID" };
  }
  const ids = (config as { claude_code_model_ids: unknown }).claude_code_model_ids;
  if (!ids || typeof ids !== "object" || Array.isArray(ids)) {
    return { ok: false, blocker_code: "MODEL_BACKEND_CONFIG_INVALID" };
  }
  const backendModel = (ids as Record<string, unknown>)[canonicalModel];
  if (backendModel === undefined) return { ok: false, blocker_code: "MODEL_IDENTIFIER_UNSUPPORTED" };
  if (typeof backendModel !== "string" || !NATIVE_ID.test(backendModel)) {
    return { ok: false, blocker_code: "MODEL_BACKEND_CONFIG_INVALID" };
  }
  return { ok: true, canonical_model: canonicalModel, backend_model: backendModel };
}

/** Classify only explicit backend evidence; ambiguous text remains invocation failure. */
export function classifyClaudeInvocationError(message: string):
  "MODEL_AUTH_UNAVAILABLE" | "MODEL_ENTITLEMENT_UNAVAILABLE" |
  "MODEL_IDENTIFIER_UNSUPPORTED" | "MODEL_INVOCATION_FAILED" {
  const value = message.toLowerCase();
  if (/\b(not logged in|login required|authentication required|unauthenticated)\b/.test(value)) {
    return "MODEL_AUTH_UNAVAILABLE";
  }
  if (/\b(your account does not have access|not available on your plan|your subscription does not include)\b/.test(value)) {
    return "MODEL_ENTITLEMENT_UNAVAILABLE";
  }
  if (/\b(invalid model identifier|unrecognized model identifier|unsupported model identifier)\b/.test(value)) {
    return "MODEL_IDENTIFIER_UNSUPPORTED";
  }
  return "MODEL_INVOCATION_FAILED";
}
