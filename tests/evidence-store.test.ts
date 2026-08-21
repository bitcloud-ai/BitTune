import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import type { ExtensionContext, ToolDefinition } from "../packages/bittune-runtime/src/core/extensions/types.ts";
import { canonicalJson, sha256 } from "../packages/bittune-capabilities/src/shared/identifiers.ts";
import { RunRecorder } from "../packages/bittune-capabilities/src/shared/run-recorder.ts";
import { StateStore } from "../packages/bittune-capabilities/src/shared/state-store.ts";
import { createToolRegistry } from "../packages/bittune-capabilities/src/tool-registry.ts";

const model = {
  model_id: "acme/meteor-8b",
  model_revision: "0123456789abcdef0123456789abcdef01234567",
  runtime_kind: "vllm" as const,
  runtime_image_repository: "registry.example/acme/vllm",
  runtime_image_digest: `sha256:${"a".repeat(64)}`,
};

async function execute(root: string, namespaceId: string, name: string, params: Record<string, unknown>) {
  const tool = createToolRegistry(root, namespaceId).find((item) => item.name === name) as ToolDefinition;
  assert.ok(tool, `missing ${name}`);
  const result = await tool.execute("tool-call-1", params as never, undefined, undefined, {
    sessionManager: { getSessionId: () => "session-1" },
  } as unknown as ExtensionContext);
  const block = result.content.find((item) => item.type === "text");
  assert.ok(block && block.type === "text");
  return JSON.parse(block.text) as Record<string, unknown>;
}

test("StateStore keeps durable evidence isolated by namespace and hashes the sanitized payload", async () => {
  const root = await mkdtemp(join(tmpdir(), "bittune-evidence-store-"));
  const primary = new StateStore(root, "project-primary");
  const other = new StateStore(root, "project-other");
  try {
    const started = await primary.startRun("inspect_gpu", { api_key: "top-secret" }, { session_id: "session-1", tool_call_id: "tool-call-1" });
    const artifact = await primary.writeArtifact(started.run_id, "provider-output", "token=top-secret\nGPU ready", "text/plain");
    const observation = {
      ok: true,
      summary: "GPU inspection complete",
      provenance_type: "measured" as const,
      measured_at: new Date().toISOString(),
      run_id: started.run_id,
      data: { api_key: "top-secret", gpu_count: 1, nested: { token: "top-secret" } },
      warnings: [],
      artifacts: [artifact],
    };
    await primary.finishRun(started.run_id, { tool_name: "inspect_gpu", started_at: started.started_at, status: "completed", provenance_type: "measured", input: { api_key: "top-secret" }, artifacts: [artifact], observation });

    const stored = await primary.getRun(started.run_id);
    assert.equal(stored.manifest.namespace_id, "project-primary");
    assert.equal(stored.manifest.session_id, "session-1");
    assert.equal(stored.manifest.tool_call_id, "tool-call-1");
    assert.equal((stored.manifest.input as Record<string, unknown>).api_key, "[REDACTED]");
    assert.equal((stored.observation.data as Record<string, unknown>).api_key, "[REDACTED]");
    assert.equal(stored.manifest.observation_hash, sha256(canonicalJson(stored.observation)));
    assert.match((await primary.readArtifact(started.run_id, artifact.artifact_id, 0, 1024)).text, /token=\[REDACTED\]/);
    await assert.rejects(other.getRun(started.run_id), (error: unknown) => (error as { stable?: { code?: string } }).stable?.code === "run_not_found");
  } finally {
    primary.close();
    other.close();
    await rm(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
});

test("query tools stay Session-only while command results use bounded projections", async () => {
  const root = await mkdtemp(join(tmpdir(), "bittune-projection-"));
  const namespaceId = "project-projection";
  const store = new StateStore(root, namespaceId);
  try {
    const query = await execute(root, namespaceId, "list_deployment_presets", {});
    assert.equal("run_id" in query, false);
    assert.deepEqual(query.query_data, { presets: [] });
    assert.deepEqual(await store.listRuns(), []);

    const command = await execute(root, namespaceId, "publish_deployment_preset", {
      preset_id: "meteor-vllm",
      ...model,
      serving_configuration: { max_model_len: 4096, max_num_seqs: 4, max_num_batched_tokens: 4096, gpu_memory_utilization: 0.8, gpu_device_ids: [0] },
    });
    assert.equal(command.status, "completed");
    assert.equal("data" in command, false);
    assert.equal(typeof command.run_id, "string");
    assert.equal(typeof command.retrieval_hint, "string");

    const stored = await store.getRun(command.run_id as string);
    assert.equal(stored.manifest.session_id, "session-1");
    assert.equal(stored.manifest.tool_call_id, "tool-call-1");
    assert.equal((stored.observation.data as Record<string, unknown>).preset_id, "meteor-vllm");
    assert.ok("serving_configuration" in (stored.observation.data as Record<string, unknown>));
  } finally {
    store.close();
    await rm(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
});

test("query projections redact sensitive values before entering Agent context", async () => {
  const store = new StateStore(await mkdtemp(join(tmpdir(), "bittune-query-redaction-")));
  try {
    const recorder = new RunRecorder(store);
    const result = await recorder.execute("query", "get_secret_fixture", "tool-call-1", {}, undefined, undefined, {} as ExtensionContext, async () => ({
      summary: "fixture",
      provenance_type: "stored",
      data: { token: "query-secret", nested: { api_key: "query-key" } },
    }));
    const block = result.content.find((item) => item.type === "text");
    assert.ok(block && block.type === "text");
    assert.deepEqual(JSON.parse(block.text).query_data, { token: "[REDACTED]", nested: { api_key: "[REDACTED]" } });
  } finally {
    const root = store.root;
    store.close();
    await rm(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
});
