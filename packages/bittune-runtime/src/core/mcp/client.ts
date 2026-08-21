import { Client, StreamableHTTPClientTransport, type Tool } from "@modelcontextprotocol/client";
import { VERSION } from "../../config.ts";
import { resolveMcpHeaders, type McpServerConfig } from "./config.ts";
import type { McpExternalTool, McpExternalToolResult, McpServerDiscovery } from "./types.ts";

function combineSignals(signal: AbortSignal | undefined, timeoutMs: number): { signal: AbortSignal; dispose: () => void } {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(new Error(`MCP request timed out after ${timeoutMs}ms.`)), timeoutMs);
  const abort = () => controller.abort(signal?.reason);
  if (signal) {
    if (signal.aborted) abort();
    else signal.addEventListener("abort", abort, { once: true });
  }
  return {
    signal: controller.signal,
    dispose: () => {
      clearTimeout(timeout);
      signal?.removeEventListener("abort", abort);
    },
  };
}

function asInputSchema(value: unknown): Record<string, unknown> | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  return value as Record<string, unknown>;
}

function asToolResult(result: Awaited<ReturnType<Client["callTool"]>>): McpExternalToolResult {
  return {
    isError: result.isError === true,
    content: result.content,
    ...(result.structuredContent === undefined ? {} : { structuredContent: result.structuredContent }),
  };
}

export class McpConnection {
  private readonly client: Client;
  private readonly transport: StreamableHTTPClientTransport;
  private connected = false;

  constructor(private readonly config: McpServerConfig) {
    this.client = new Client({ name: "bittune", version: VERSION });
    this.transport = new StreamableHTTPClientTransport(new URL(config.url), {
      requestInit: { headers: resolveMcpHeaders(config) },
      onInsufficientScope: "throw",
      reconnectionOptions: {
        initialReconnectionDelay: 250,
        maxReconnectionDelay: 1_000,
        reconnectionDelayGrowFactor: 1,
        maxRetries: 0,
      },
    });
  }

  async discover(signal?: AbortSignal): Promise<McpServerDiscovery> {
    const timeout = combineSignals(signal, this.config.timeoutMs);
    try {
      await this.client.connect(this.transport, { signal: timeout.signal });
      this.connected = true;
      const listed = await this.client.listTools(undefined, { signal: timeout.signal, cacheMode: "refresh" });
      const remoteTools = listed.tools
        .map((tool) => this.externalTool(tool))
        .filter((tool): tool is McpExternalTool => tool !== undefined);
      const allowed = new Set(this.config.allowTools);
		const discoveredToolNames = listed.tools.map((tool) => tool.name);
      return {
        serverName: this.config.name,
        discoveredToolNames,
        missingAllowedToolNames: this.config.allowTools.filter((name) => !discoveredToolNames.includes(name)),
        tools: remoteTools.filter((tool) => allowed.has(tool.remoteName)),
      };
    } catch (error) {
      await this.close();
      throw error;
    } finally {
      timeout.dispose();
    }
  }

  async close(): Promise<void> {
    this.connected = false;
    await this.client.close().catch(() => undefined);
  }

  private externalTool(tool: Tool): McpExternalTool | undefined {
    const inputSchema = asInputSchema(tool.inputSchema);
    if (!inputSchema) return undefined;
    return {
      serverName: this.config.name,
      remoteName: tool.name,
      description: typeof tool.description === "string" ? tool.description : "External MCP tool.",
      inputSchema,
      ...(this.config.toolHints[tool.name] ? { hint: this.config.toolHints[tool.name] } : {}),
      argumentBindings: this.config.argumentBindings[tool.name] ?? {},
      call: async (argumentsValue, signal) => this.call(tool, argumentsValue, signal),
    };
  }

  private async call(tool: Tool, argumentsValue: Record<string, unknown>, signal?: AbortSignal): Promise<McpExternalToolResult> {
    if (!this.connected) throw new Error(`MCP server ${this.config.name} is not connected.`);
    const timeout = combineSignals(signal, this.config.timeoutMs);
    try {
      const result = await this.client.callTool({ name: tool.name, arguments: argumentsValue }, { signal: timeout.signal, toolDefinition: tool });
      return asToolResult(result);
    } finally {
      timeout.dispose();
    }
  }
}

export async function discoverMcpServer(config: McpServerConfig, signal?: AbortSignal): Promise<{ discovery: McpServerDiscovery; connection: McpConnection }> {
  const connection = new McpConnection(config);
  try {
    return { discovery: await connection.discover(signal), connection };
  } catch (error) {
    await connection.close();
    throw error;
  }
}
