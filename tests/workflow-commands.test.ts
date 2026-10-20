import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";
import { WORKFLOW_COMMANDS } from "../extensions/workflow-commands.ts";

const ROOT = join(import.meta.dirname, "..");
const MISSION_COMMANDS = ["deliver","deliver-status","deliver-link","deliver-pause","deliver-resume","deliver-cancel"];

test("documented workflow commands match exported registry", () => {
  const readme = readFileSync(join(ROOT, "README.md"), "utf8");
  const marker = "<!-- workflow-command-registry -->";
  const line = readme.split("\n").find((value) => value.startsWith(marker));
  assert.ok(line, "README must contain the machine-checked workflow command registry");
  const documented = line.slice(marker.length).trim().split(/\s+/).filter(Boolean).map(x=>x.replace(/^\//,""));
  assert.deepEqual(documented, [...WORKFLOW_COMMANDS]);
});

test("launcher loads every command-providing extension", () => {
  const helper = readFileSync(join(ROOT, "scripts", "use-latest.sh"), "utf8");
  assert.match(helper, /extensions\/workflow-commands\.ts/);
  assert.match(helper, /extensions\/mission-controller\.ts/);
  for (const command of MISSION_COMMANDS) {
    const mission = readFileSync(join(ROOT, "extensions", "mission-controller.ts"), "utf8");
    assert.ok(mission.includes(`registerCommand("${command}"`), `missing /${command}`);
  }
});
