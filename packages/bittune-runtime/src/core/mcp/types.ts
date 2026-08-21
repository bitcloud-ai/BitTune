export interface McpExternalToolResult {
  isError: boolean;
  content: unknown;
  structuredContent?: unknown;
}

export interface McpExternalTool {
  serverName: string;
  remoteName: string;
  description: string;
  inputSchema: Record<string, unknown>;
  hint?: string;
  argumentBindings: Readonly<Record<string, McpArgumentBinding>>;
  call: (argumentsValue: Record<string, unknown>, signal?: AbortSignal) => Promise<McpExternalToolResult>;
}

export interface McpDiagnostic {
  serverName: string;
  code: string;
  message: string;
}

export interface McpServerDiscovery {
  serverName: string;
  tools: McpExternalTool[];
  discoveredToolNames: string[];
  missingAllowedToolNames: string[];
}

export interface McpDiscoveryResult {
  servers: McpServerDiscovery[];
  diagnostics: McpDiagnostic[];
  close: () => Promise<void>;
}
import type { McpArgumentBinding } from "./config.ts";
