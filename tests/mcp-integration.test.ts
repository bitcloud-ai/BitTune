import assert from "node:assert/strict";
import { createServer, type Server } from "node:http";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import type { ExtensionContext } from "../packages/bittune-runtime/src/core/extensions/types.ts";
import { createBittuneExtension } from "../packages/bittune-capabilities/src/index.ts";
import { createMcpTools } from "../packages/bittune-capabilities/src/mcp/tools.ts";
import { RunRecorder } from "../packages/bittune-capabilities/src/shared/run-recorder.ts";
import { StateStore } from "../packages/bittune-capabilities/src/shared/state-store.ts";
import { parseBittuneCliArguments } from "../packages/bittune-runtime/src/cli/bittune-args.ts";
import { parseMcpConfiguration, resolveMcpHeaders } from "../packages/bittune-runtime/src/core/mcp/config.ts";
import { discoverConfiguredMcpTools } from "../packages/bittune-runtime/src/core/mcp/discovery.ts";
import type { McpExternalTool } from "../packages/bittune-runtime/src/core/mcp/types.ts";
import { CORE_TOOL_NAMES } from "../packages/bittune-capabilities/src/shared/capability-catalog.ts";

async function readBody(request: Parameters<Server["emit"]>[1]): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of request as AsyncIterable<Buffer>) chunks.push(Buffer.from(chunk));
  return Buffer.concat(chunks).toString("utf8");
}

async function startMockMcpServer(): Promise<{ server: Server; url: string; calls: string[] }> {
  const calls: string[] = [];
  const server = createServer(async (request, response) => {
    if (request.method !== "POST") {
      response.writeHead(405).end();
      return;
    }
    const message = JSON.parse(await readBody(request)) as { id?: number | string; method: string; params?: Record<string, unknown> };
    calls.push(message.method);
    if (message.id === undefined) {
      response.writeHead(202).end();
      return;
    }
    const send = (result: unknown) => {
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({ jsonrpc: "2.0", id: message.id, result }));
    };
    if (message.method === "server/discover") {
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({ jsonrpc: "2.0", id: message.id, error: { code: -32601, message: "Method not found" } }));
      return;
    }
    if (message.method === "initialize") {
      send({ protocolVersion: "2025-06-18", capabilities: { tools: {} }, serverInfo: { name: "mock-mcp", version: "1.0.0" } });
      return;
    }
    if (message.method === "tools/list") {
      send({
        tools: [
          { name: "ragflow_retrieval", description: "Search deployment experience", inputSchema: { type: "object", properties: { question: { type: "string" } }, required: ["question"], additionalProperties: false } },
          { name: "forbidden_tool", description: "Must not be registered", inputSchema: { type: "object", properties: {}, additionalProperties: false } },
        ],
      });
      return;
    }
    if (message.method === "tools/call") {
      const argumentsValue = message.params?.arguments as { question?: string } | undefined;
      send({ content: [{ type: "text", text: `experience for ${argumentsValue?.question ?? "unknown"}` }] });
      return;
    }
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({ jsonrpc: "2.0", id: message.id, error: { code: -32601, message: "Method not found" } }));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  return { server, url: `http://127.0.0.1:${address.port}/mcp`, calls };
}

test("MCP configuration accepts only environment-backed read-only Streamable HTTP servers", () => {
  const configuration = parseMcpConfiguration({
    mcpServers: {
      ragflow: {
        enabled: true,
        transport: "streamable-http",
        url: "http://127.0.0.1:9382/mcp",
        headers: { Authorization: "Bearer ${BITTUNE_RAGFLOW_API_KEY}" },
        allowTools: ["ragflow_retrieval"],
        effect: "read-only",
      },
    },
  });
  assert.equal(configuration.servers.length, 1);
  assert.deepEqual(resolveMcpHeaders(configuration.servers[0]!, { BITTUNE_RAGFLOW_API_KEY: "secret" }), { Authorization: "Bearer secret" });
  assert.throws(() => parseMcpConfiguration({ mcpServers: { unsafe: { transport: "streamable-http", url: "https://example.com/mcp", headers: { Authorization: "Bearer plaintext-secret" }, allowTools: ["read"], effect: "read-only" } } }), /environment variable/);
  assert.throws(() => parseMcpConfiguration({ mcpServers: { unsafe: { transport: "sse", url: "https://example.com/mcp", allowTools: ["read"], effect: "read-only" } } }), /streamable-http/);
});

test("MCP discovery follows allowTools and the bridge remains a non-recording query", async () => {
  const mock = await startMockMcpServer();
  const root = await mkdtemp(join(tmpdir(), "bittune-mcp-"));
  try {
    const configuration = parseMcpConfiguration({
      mcpServers: {
        ragflow: {
          transport: "streamable-http",
          url: mock.url,
          allowTools: ["ragflow_retrieval", "missing_tool"],
          effect: "read-only",
          toolHints: { ragflow_retrieval: "Use for deployment experience." },
        },
      },
    });
    const discovery = await discoverConfiguredMcpTools(configuration);
    try {
      assert.deepEqual(discovery.servers[0]?.tools.map((tool) => tool.remoteName), ["ragflow_retrieval"]);
      assert.deepEqual(discovery.servers[0]?.missingAllowedToolNames, ["missing_tool"]);
      assert.equal(discovery.diagnostics[0]?.code, "allowed_tool_not_found");
      const bridged = createMcpTools(new RunRecorder(new StateStore(root)), discovery.servers.flatMap((server) => server.tools));
      assert.deepEqual(bridged.tools.map((tool) => tool.name), ["mcp_ragflow__ragflow_retrieval"]);
      const result = await bridged.tools[0]!.execute("mcp-call", { question: "How should I configure vLLM?" } as never, undefined, undefined, {} as ExtensionContext);
      const text = result.content.find((item) => item.type === "text");
      assert.ok(text && text.type === "text");
      const projection = JSON.parse(text.text) as { run_id?: string; status: string; query_data?: { external_reference?: boolean; content?: Array<{ text?: string }> } };
      assert.equal(projection.run_id, undefined);
      assert.equal(projection.status, "completed");
      assert.equal(projection.query_data?.external_reference, true);
      assert.match(projection.query_data?.content?.[0]?.text ?? "", /vLLM/);
      const store = new StateStore(root);
      try {
        assert.deepEqual(await store.listRuns(), []);
      } finally {
        store.close();
      }
      const invalid = await bridged.tools[0]!.execute("mcp-invalid", {} as never, undefined, undefined, {} as ExtensionContext);
      const invalidText = invalid.content.find((item) => item.type === "text");
      assert.ok(invalidText && invalidText.type === "text");
      assert.equal((JSON.parse(invalidText.text) as { status: string }).status, "failed");
      assert.ok(mock.calls.includes("initialize"));
      assert.ok(mock.calls.includes("tools/list"));
      assert.ok(mock.calls.includes("tools/call"));
    } finally {
      await discovery.close();
    }
  } finally {
    await new Promise<void>((resolve, reject) => mock.server.close((error) => error ? reject(error) : resolve()));
    await rm(root, { recursive: true, force: true });
  }
});

test("MCP bridge refuses invalid schemas and preserves explicit namespacing", () => {
  const external: McpExternalTool = {
    serverName: "deployment-recommendation",
    remoteName: "recommend_model",
    description: "Recommend a model.",
    inputSchema: { type: "object", properties: { gpu: { type: "string" } }, required: ["gpu"] },
    argumentBindings: {},
    call: async () => ({ isError: false, content: [] }),
  };
  const bridged = createMcpTools(new RunRecorder(new StateStore(join(tmpdir(), "bittune-mcp-name"))), [external]);
  assert.deepEqual(bridged.tools.map((tool) => tool.name), ["mcp_deployment_recommendation__recommend_model"]);
});

test("MCP tool-level errors become structured failed query results", async () => {
  const external: McpExternalTool = {
    serverName: "ragflow",
    remoteName: "ragflow_retrieval",
    description: "Search deployment experience.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    argumentBindings: {},
    call: async () => ({ isError: true, content: [{ type: "text", text: "upstream failure" }] }),
  };
  const [tool] = createMcpTools(new RunRecorder(new StateStore(join(tmpdir(), "bittune-mcp-error"))), [external]).tools;
  assert.ok(tool);
  const result = await tool!.execute("mcp-error", {} as never, undefined, undefined, {} as ExtensionContext);
  const text = result.content.find((item) => item.type === "text");
  assert.ok(text && text.type === "text");
  const projection = JSON.parse(text.text) as { status: string; summary: string };
  assert.equal(projection.status, "failed");
  assert.match(projection.summary, /ragflow/);
});

test("enabled external MCP tools are visible without Capability activation", async () => {
  const external: McpExternalTool = {
    serverName: "ragflow",
    remoteName: "ragflow_retrieval",
    description: "Search deployment experience.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    argumentBindings: {},
    call: async () => ({ isError: false, content: [] }),
  };
  const [tool] = createMcpTools(new RunRecorder(new StateStore(join(tmpdir(), "bittune-mcp-visible"))), [external]).tools;
  assert.ok(tool);
  const activeToolSets: string[][] = [];
  let sessionStart: ((event: unknown, context: { sessionManager: { getBranch: () => readonly unknown[] } }) => Promise<void>) | undefined;
  createBittuneExtension({ externalTools: [tool!] })({
    registerTool: () => undefined,
    setActiveTools: (names: string[]) => activeToolSets.push(names),
    appendEntry: () => undefined,
    on: (event: string, handler: typeof sessionStart) => { if (event === "session_start") sessionStart = handler; },
  } as never);
  assert.ok(sessionStart);
  await sessionStart!({ type: "session_start" }, { sessionManager: { getBranch: () => [] } });
  assert.deepEqual(activeToolSets.at(-1), [...CORE_TOOL_NAMES, "mcp_ragflow__ragflow_retrieval"]);
});

test("MCP argument bindings override Agent-supplied hardware with latest local evidence", async () => {
  const root = await mkdtemp(join(tmpdir(), "bittune-mcp-binding-"));
  let receivedHardware: unknown;
  try {
    const seed = new StateStore(root);
    await seed.initialize();
    const started = await seed.startRun("inspect_gpu", {});
    await seed.finishRun(started.run_id, {
      tool_name: "inspect_gpu",
      started_at: started.started_at,
      status: "completed",
      provenance_type: "measured",
      input: {},
      artifacts: [],
      observation: { ok: true, summary: "GPU measured", provenance_type: "measured", measured_at: new Date().toISOString(), run_id: started.run_id, data: { normalized_hardware: { gpus: [{ name: "Measured GPU", memory_bytes: 48 }] } }, warnings: [], artifacts: [] },
    });
    seed.close();
    const external: McpExternalTool = {
      serverName: "deployment-recommendation",
      remoteName: "recommend_model_for_hardware",
      description: "Recommend a model.",
      inputSchema: { type: "object", properties: { hardware: { type: "object" } }, required: ["hardware"], additionalProperties: false },
      argumentBindings: {
        hardware: { source: "latest-bittune-observation", toolName: "inspect_gpu", dataPointer: "/normalized_hardware", maxAgeSeconds: 300 },
      },
      call: async (argumentsValue) => {
        receivedHardware = argumentsValue.hardware;
        return { isError: false, content: [{ type: "text", text: "recommendation" }] };
      },
    };
    const bridged = createMcpTools(new RunRecorder(new StateStore(root)), [external]);
    const result = await bridged.tools[0]!.execute("recommend", { hardware: { gpus: [{ name: "Invented GPU" }] } } as never, undefined, undefined, {} as ExtensionContext);
    assert.equal(result.content.some((item) => item.type === "text"), true);
    assert.deepEqual(receivedHardware, { gpus: [{ name: "Measured GPU", memory_bytes: 48 }] });
  } finally {
    await rm(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
});

test("MCP CLI parsing exposes only management and diagnostic operations", () => {
  assert.deepEqual(parseBittuneCliArguments(["mcp", "list"]), { kind: "mcp", command: { kind: "list" } });
  assert.deepEqual(parseBittuneCliArguments(["mcp", "get", "ragflow"]), { kind: "mcp", command: { kind: "get", name: "ragflow" } });
  assert.deepEqual(parseBittuneCliArguments(["mcp", "test"]), { kind: "mcp", command: { kind: "test" } });
  assert.deepEqual(parseBittuneCliArguments(["mcp", "test", "ragflow"]), { kind: "mcp", command: { kind: "test", name: "ragflow" } });
  assert.throws(() => parseBittuneCliArguments(["mcp", "remove", "ragflow"]), /Unknown mcp command/);
});
