import test from "node:test";
import assert from "node:assert/strict";
import { buildWorkerEnv } from "../extensions/claude-worker.ts";

test("workers inherit exactly the four explicit Git identity fields without Git credentials or overrides", () => {
  const identity = { GIT_AUTHOR_NAME: "Approved Author Name", GIT_AUTHOR_EMAIL: "author@example.test",
    GIT_COMMITTER_NAME: "Approved Committer Name", GIT_COMMITTER_EMAIL: "committer@example.test" };
  const withheld = { GIT_ASKPASS: "/secret-helper", GIT_SSH_COMMAND: "ssh -i secret", GIT_DIR: "/other/repo",
    GIT_CONFIG_COUNT: "1", GIT_CONFIG_KEY_0: "credential.helper", GIT_CONFIG_VALUE_0: "secret",
    GITHUB_TOKEN: "secret", GH_TOKEN: "secret", SSH_AUTH_SOCK: "/secret-socket" };
  const result = buildWorkerEnv({ ...identity, ...withheld });
  assert.deepEqual(result.env, identity);
  assert.deepEqual(new Set(result.dropped), new Set(Object.keys(withheld)));
});
