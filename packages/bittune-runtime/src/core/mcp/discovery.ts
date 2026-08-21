import { discoverMcpServer } from "./client.ts";
import type { McpConfiguration } from "./config.ts";
import type { McpDiagnostic, McpDiscoveryResult, McpServerDiscovery } from "./types.ts";

function stableMessage(error: unknown): string {
  const text = error instanceof Error ? error.message : String(error);
  return text.length <= 400 ? text : `${text.slice(0, 400)}...`;
}

function diagnostic(serverName: string, error: unknown): McpDiagnostic {
  const message = stableMessage(error);
  const code = /environment variable/i.test(message) ? "missing_environment" : /timed out|abort/i.test(message) ? "timeout" : "connection_failed";
  return { serverName, code, message };
}

export async function discoverConfiguredMcpTools(configuration: McpConfiguration, signal?: AbortSignal): Promise<McpDiscoveryResult> {
  const enabled = configuration.servers.filter((server) => server.enabled);
  const discovered = await Promise.all(enabled.map(async (server) => {
    try {
      return { result: await discoverMcpServer(server, signal) };
    } catch (error) {
      return { error: diagnostic(server.name, error) };
    }
  }));
  const servers: McpServerDiscovery[] = [];
  const diagnostics: McpDiagnostic[] = [];
  const closeConnections: Array<() => Promise<void>> = [];
  for (const item of discovered) {
    if (item.result) {
      servers.push(item.result.discovery);
      closeConnections.push(() => item.result!.connection.close());
		if (item.result.discovery.missingAllowedToolNames.length > 0) {
			diagnostics.push({
				serverName: item.result.discovery.serverName,
				code: "allowed_tool_not_found",
				message: `Configured allowTools were not returned by the server: ${item.result.discovery.missingAllowedToolNames.join(", ")}.`,
			});
		}
    }
    else if (item.error) diagnostics.push(item.error);
  }
  return {
    servers,
    diagnostics,
    close: async () => { await Promise.all(closeConnections.map((close) => close())); },
  };
}
