import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { recordBashAudit } from "../packages/bittune-capabilities/src/shared/bash-audit.ts";
import { StateStore } from "../packages/bittune-capabilities/src/shared/state-store.ts";

test("Bash tool results do not create Bittune Evidence Runs", async () => {
  const root = await mkdtemp(join(tmpdir(), "bittune-bash-audit-"));
  try {
    await recordBashAudit(root, {
      type: "tool_result",
      toolName: "bash",
      toolCallId: "bash-call",
      input: { command: "docker login --password secret-value" },
      content: [{ type: "text", text: "token=secret-value\nimage ready" }],
      details: undefined,
      isError: false,
    });
    const store = new StateStore(root);
    assert.deepEqual(await store.listRuns({ tool_name: "bash" }), []);
    store.close();
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
