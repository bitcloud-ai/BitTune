import { readFile } from "node:fs/promises";

const ENVIRONMENT_VARIABLE = /^[A-Z][A-Z0-9_]*$/;
const SERVER_NAME = /^[a-z][a-z0-9-]{0,62}$/;
const REMOTE_TOOL_NAME = /^[A-Za-z][A-Za-z0-9_-]{0,127}$/;
const HEADER_NAME = /^[A-Za-z0-9-]+$/;
const HEADER_VALUE = /^(?:[A-Za-z-]+ )?(?:\$\{([A-Z][A-Z0-9_]*)\}|\$([A-Z][A-Z0-9_]*))$/;

export interface McpServerConfig {
  name: string;
  enabled: boolean;
  transport: "streamable-http";
  url: string;
  headers: Readonly<Record<string, string>>;
  allowTools: readonly string[];
  effect: "read-only";
  timeoutMs: number;
  toolHints: Readonly<Record<string, string>>;
  argumentBindings: Readonly<Record<string, Readonly<Record<string, McpArgumentBinding>>>>;
}

export interface McpArgumentBinding {
  source: "latest-bittune-observation";
  toolName: string;
  /** RFC 6901 JSON Pointer inside the source Observation data. Empty selects the complete data object. */
  dataPointer: string;
  maxAgeSeconds?: number;
}

export interface McpConfiguration {
  servers: readonly McpServerConfig[];
}

export class McpConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "McpConfigError";
  }
}

function asRecord(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new McpConfigError(`${label} must be an object.`);
  return value as Record<string, unknown>;
}

function asOptionalBoolean(value: unknown, label: string, fallback: boolean): boolean {
  if (value === undefined) return fallback;
  if (typeof value !== "boolean") throw new McpConfigError(`${label} must be a boolean.`);
  return value;
}

function asString(value: unknown, label: string): string {
  if (typeof value !== "string" || !value.trim()) throw new McpConfigError(`${label} must be a non-empty string.`);
  return value.trim();
}

function asStringArray(value: unknown, label: string): string[] {
  if (!Array.isArray(value) || value.length === 0) throw new McpConfigError(`${label} must be a non-empty string array.`);
  const values = value.map((item, index) => asString(item, `${label}[${index}]`));
  if (new Set(values).size !== values.length) throw new McpConfigError(`${label} must not contain duplicates.`);
  return values;
}

function parseUrl(value: unknown, label: string): string {
  const raw = asString(value, label);
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new McpConfigError(`${label} must be an absolute HTTP(S) URL.`);
  }
  if (!url.hostname || (url.protocol !== "http:" && url.protocol !== "https:")) {
    throw new McpConfigError(`${label} must be an absolute HTTP(S) URL.`);
  }
  return url.toString();
}

function parseHeaders(value: unknown, label: string): Record<string, string> {
  if (value === undefined) return {};
  const headers = asRecord(value, label);
  const parsed: Record<string, string> = {};
  for (const [name, rawValue] of Object.entries(headers)) {
    if (!HEADER_NAME.test(name)) throw new McpConfigError(`${label}.${name} has an invalid header name.`);
    const headerValue = asString(rawValue, `${label}.${name}`);
    if (!HEADER_VALUE.test(headerValue)) {
      throw new McpConfigError(`${label}.${name} must reference exactly one environment variable; literal credentials are not allowed.`);
    }
    parsed[name] = headerValue;
  }
  return parsed;
}

function parseToolHints(value: unknown, label: string, allowTools: readonly string[]): Record<string, string> {
  if (value === undefined) return {};
  const hints = asRecord(value, label);
  const parsed: Record<string, string> = {};
  for (const [toolName, hint] of Object.entries(hints)) {
    if (!allowTools.includes(toolName)) throw new McpConfigError(`${label}.${toolName} is not listed in allowTools.`);
    const text = asString(hint, `${label}.${toolName}`);
    if (text.length > 1_000) throw new McpConfigError(`${label}.${toolName} exceeds 1000 characters.`);
    parsed[toolName] = text;
  }
  return parsed;
}

function parseBinding(value: unknown, label: string): McpArgumentBinding {
  const raw = asRecord(value, label);
  if (raw.source !== "latest-bittune-observation") throw new McpConfigError(`${label}.source must be "latest-bittune-observation".`);
  const toolName = asString(raw.toolName, `${label}.toolName`);
  if (!REMOTE_TOOL_NAME.test(toolName)) throw new McpConfigError(`${label}.toolName is invalid.`);
  const dataPointer = raw.dataPointer === undefined ? "" : asString(raw.dataPointer, `${label}.dataPointer`);
  if (dataPointer && (!dataPointer.startsWith("/") || dataPointer.length > 256)) throw new McpConfigError(`${label}.dataPointer must be an RFC 6901 JSON Pointer.`);
  const maxAgeSeconds = raw.maxAgeSeconds;
  if (maxAgeSeconds !== undefined && (typeof maxAgeSeconds !== "number" || !Number.isInteger(maxAgeSeconds) || maxAgeSeconds < 1 || maxAgeSeconds > 86_400)) {
    throw new McpConfigError(`${label}.maxAgeSeconds must be an integer from 1 to 86400.`);
  }
  return { source: "latest-bittune-observation", toolName, dataPointer, ...(maxAgeSeconds === undefined ? {} : { maxAgeSeconds }) };
}

function parseArgumentBindings(value: unknown, label: string, allowTools: readonly string[]): Record<string, Record<string, McpArgumentBinding>> {
  if (value === undefined) return {};
  const tools = asRecord(value, label);
  const parsed: Record<string, Record<string, McpArgumentBinding>> = {};
  for (const [toolName, rawBindings] of Object.entries(tools)) {
    if (!allowTools.includes(toolName)) throw new McpConfigError(`${label}.${toolName} is not listed in allowTools.`);
    const bindings = asRecord(rawBindings, `${label}.${toolName}`);
    const mapped: Record<string, McpArgumentBinding> = {};
    for (const [argumentName, rawBinding] of Object.entries(bindings)) {
      if (!REMOTE_TOOL_NAME.test(argumentName)) throw new McpConfigError(`${label}.${toolName}.${argumentName} has an invalid argument name.`);
      mapped[argumentName] = parseBinding(rawBinding, `${label}.${toolName}.${argumentName}`);
    }
    parsed[toolName] = mapped;
  }
  return parsed;
}

function parseServer(name: string, value: unknown): McpServerConfig {
  if (!SERVER_NAME.test(name)) throw new McpConfigError(`mcpServers.${name} has an invalid server name.`);
  const raw = asRecord(value, `mcpServers.${name}`);
  const allowTools = asStringArray(raw.allowTools, `mcpServers.${name}.allowTools`);
  for (const toolName of allowTools) {
    if (!REMOTE_TOOL_NAME.test(toolName)) throw new McpConfigError(`mcpServers.${name}.allowTools contains an invalid MCP tool name.`);
  }
  if (raw.transport !== "streamable-http") throw new McpConfigError(`mcpServers.${name}.transport must be "streamable-http" in Bittune MCP v1.`);
  if (raw.effect !== "read-only") throw new McpConfigError(`mcpServers.${name}.effect must be "read-only" in Bittune MCP v1.`);
  const configuredTimeout = raw.timeoutMs ?? 10_000;
  if (typeof configuredTimeout !== "number" || !Number.isInteger(configuredTimeout) || configuredTimeout < 1_000 || configuredTimeout > 120_000) {
    throw new McpConfigError(`mcpServers.${name}.timeoutMs must be an integer from 1000 to 120000.`);
  }
  return {
    name,
    enabled: asOptionalBoolean(raw.enabled, `mcpServers.${name}.enabled`, true),
    transport: "streamable-http",
    url: parseUrl(raw.url, `mcpServers.${name}.url`),
    headers: parseHeaders(raw.headers, `mcpServers.${name}.headers`),
    allowTools,
    effect: "read-only",
    timeoutMs: configuredTimeout,
    toolHints: parseToolHints(raw.toolHints, `mcpServers.${name}.toolHints`, allowTools),
    argumentBindings: parseArgumentBindings(raw.argumentBindings, `mcpServers.${name}.argumentBindings`, allowTools),
  };
}

export function parseMcpConfiguration(value: unknown): McpConfiguration {
  const root = asRecord(value, "mcp.json");
  const rawServers = root.mcpServers;
  if (rawServers === undefined) return { servers: [] };
  const serversRecord = asRecord(rawServers, "mcpServers");
  const servers = Object.entries(serversRecord).map(([name, server]) => parseServer(name, server));
  return { servers };
}

export async function loadMcpConfiguration(path: string): Promise<McpConfiguration> {
  let text: string;
  try {
    text = await readFile(path, "utf8");
  } catch (error: unknown) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return { servers: [] };
    throw error;
  }
  try {
    return parseMcpConfiguration(JSON.parse(text) as unknown);
  } catch (error: unknown) {
    if (error instanceof McpConfigError) throw error;
    throw new McpConfigError(`Invalid JSON in ${path}: ${error instanceof Error ? error.message : String(error)}`);
  }
}

export function resolveMcpHeaders(config: McpServerConfig, environment: NodeJS.ProcessEnv = process.env): Record<string, string> {
  const headers: Record<string, string> = {};
  for (const [name, template] of Object.entries(config.headers)) {
    const match = HEADER_VALUE.exec(template);
    const environmentName = match?.[1] ?? match?.[2];
    if (!environmentName || !ENVIRONMENT_VARIABLE.test(environmentName)) throw new McpConfigError(`mcpServers.${config.name}.headers.${name} has an invalid environment variable reference.`);
    const value = environment[environmentName]?.trim();
    if (!value) throw new McpConfigError(`mcpServers.${config.name}.headers.${name} requires environment variable ${environmentName}.`);
    headers[name] = template.replace(/\$\{[A-Z][A-Z0-9_]*\}|\$[A-Z][A-Z0-9_]*/, value);
  }
  return headers;
}

export function publicMcpServerConfig(config: McpServerConfig): Record<string, unknown> {
  return {
    name: config.name,
    enabled: config.enabled,
    transport: config.transport,
    url: config.url,
    headers: Object.fromEntries(Object.entries(config.headers).map(([name, value]) => [name, value.replace(HEADER_VALUE, "$ENV")])),
    allowTools: config.allowTools,
    effect: config.effect,
    timeoutMs: config.timeoutMs,
    toolHints: config.toolHints,
    argumentBindings: config.argumentBindings,
  };
}
