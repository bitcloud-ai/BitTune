import { chmod, mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createBittuneExtension } from "../../bittune-capabilities/src/index.ts";
import { createMcpTools } from "../../bittune-capabilities/src/mcp/tools.ts";
import { RunRecorder } from "../../bittune-capabilities/src/shared/run-recorder.ts";
import { namespaceForWorkspace } from "../../bittune-capabilities/src/shared/state-store.ts";
import { StateStore } from "../../bittune-capabilities/src/shared/state-store.ts";
import {
	CORE_TOOL_NAMES,
	activeToolNamesForCapabilities,
	capabilitiesFromSessionEntries,
} from "../../bittune-capabilities/src/shared/capability-catalog.ts";
import { BITTUNE_SYSTEM_PROMPT } from "../../bittune-capabilities/src/shared/system-prompt.ts";
import { createToolRegistry } from "../../bittune-capabilities/src/tool-registry.ts";
import { createAgentSessionRuntime, type CreateAgentSessionRuntimeFactory } from "./core/agent-session-runtime.ts";
import type { AgentSessionRuntimeDiagnostic, AgentSessionServices } from "./core/agent-session-services.ts";
import { createAgentSession } from "./core/sdk.ts";
import { ModelRuntime } from "./core/model-runtime.ts";
import { DefaultResourceLoader } from "./core/resource-loader.ts";
import { assertValidSessionId, SessionManager } from "./core/session-manager.ts";
import { SettingsManager } from "./core/settings-manager.ts";
import { parseBittuneCliArguments, type BittuneConfigureOptions, type BittuneMcpCommand } from "./cli/bittune-args.ts";
import { InteractiveMode } from "./modes/interactive/interactive-mode.ts";
import { VERSION } from "./config.ts";
import { loadMcpConfiguration, publicMcpServerConfig } from "./core/mcp/config.ts";
import { discoverConfiguredMcpTools } from "./core/mcp/discovery.ts";

export interface BittunePaths {
	root: string;
	agent: string;
	sessions: string;
	state: string;
	logs: string;
}

export interface CreateBittuneRuntimeOptions {
	cwd?: string;
	paths?: Partial<BittunePaths>;
	sessionId?: string;
}

const AGENT_CONFIG_MARKER = "bittune-runtime.json";
const AGENT_CONFIG_SCHEMA_VERSION = 1;

function getEnvironmentPath(name: string): string | undefined {
	const value = process.env[name]?.trim();
	return value || undefined;
}

export function resolveBittunePaths(overrides: Partial<BittunePaths> = {}): BittunePaths {
	const root = overrides.root ?? getEnvironmentPath("BITTUNE_HOME") ?? join(homedir(), ".bittune");
	const agent =
		overrides.agent ??
		getEnvironmentPath("BITTUNE_AGENT_DIR") ??
		getEnvironmentPath("BITTUNE_CODING_AGENT_DIR") ??
		join(root, "agent");
	return {
		root,
		agent,
		sessions:
			overrides.sessions ?? getEnvironmentPath("BITTUNE_SESSION_DIR") ?? join(agent, "sessions"),
		state: overrides.state ?? getEnvironmentPath("BITTUNE_STATE_DIR") ?? join(root, "state"),
		logs: overrides.logs ?? getEnvironmentPath("BITTUNE_LOG_DIR") ?? join(root, "logs"),
	};
}

async function ensureDirectories(paths: BittunePaths): Promise<void> {
	await Promise.all([paths.agent, paths.sessions, paths.state, paths.logs].map((path) => mkdir(path, { recursive: true })));
}

async function isBittuneAgentConfigurationInitialized(agentDir: string): Promise<boolean> {
	try {
		const marker = JSON.parse(await readFile(join(agentDir, AGENT_CONFIG_MARKER), "utf8")) as Record<string, unknown>;
		return marker.schema_version === AGENT_CONFIG_SCHEMA_VERSION;
	} catch (error: unknown) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return false;
		throw new Error(`Bittune Agent configuration marker is invalid: ${(error as Error).message}`);
	}
}

async function writeBittuneAgentConfigurationMarker(agentDir: string): Promise<void> {
	const markerPath = join(agentDir, AGENT_CONFIG_MARKER);
	await writeFile(
		markerPath,
		`${JSON.stringify({ schema_version: AGENT_CONFIG_SCHEMA_VERSION, initialized_at: new Date().toISOString() })}\n`,
		{ mode: 0o600 },
	);
	await chmod(markerPath, 0o600).catch(() => undefined);
}

async function assertBittuneAgentConfigurationInitialized(agentDir: string): Promise<void> {
	if (await isBittuneAgentConfigurationInitialized(agentDir)) return;
	throw new Error(
		"Bittune is not initialized for this Runtime. Run `bittune configure --base-url <url> --model-id <id>`; Bittune does not import previous Agent configuration.",
	);
}

function createRuntimeFactory(paths: BittunePaths): CreateAgentSessionRuntimeFactory {
	return async ({ cwd, agentDir, sessionManager, sessionStartEvent }) => {
		const namespaceId = namespaceForWorkspace(cwd);
		const domainTools = createToolRegistry(paths.state, namespaceId);
		const restoredCapabilities = capabilitiesFromSessionEntries(sessionManager.getBranch());
		const diagnostics: AgentSessionRuntimeDiagnostic[] = [];
		let mcpTools: ReturnType<typeof createMcpTools>["tools"] = [];
		let closeMcpTools: (() => Promise<void>) | undefined;
		try {
			const mcpConfiguration = await loadMcpConfiguration(join(paths.root, "mcp.json"));
			const discoveredMcp = await discoverConfiguredMcpTools(mcpConfiguration);
			for (const diagnostic of discoveredMcp.diagnostics) {
				diagnostics.push({ type: "warning", message: `MCP server ${diagnostic.serverName} unavailable (${diagnostic.code}): ${diagnostic.message}` });
			}
			const externalTools = discoveredMcp.servers.flatMap((server) => server.tools);
			const bridged = createMcpTools(new RunRecorder(new StateStore(paths.state, namespaceId)), externalTools);
			mcpTools = bridged.tools;
			closeMcpTools = discoveredMcp.close;
			for (const diagnostic of bridged.diagnostics) diagnostics.push({ type: "warning", message: `MCP bridge: ${diagnostic}` });
		} catch (error) {
			diagnostics.push({ type: "warning", message: `MCP configuration unavailable: ${error instanceof Error ? error.message : String(error)}` });
		}
		const availableTools = [
			...new Set([...CORE_TOOL_NAMES, ...domainTools.map((tool) => tool.name), ...mcpTools.map((tool) => tool.name)]),
		];
		const settingsManager = SettingsManager.create(cwd, agentDir, { projectTrusted: false });
		const modelRuntime = await ModelRuntime.create({
			authPath: join(agentDir, "auth.json"),
			modelsPath: join(agentDir, "models.json"),
			refreshOnCreate: false,
			allowModelNetwork: false,
		});
		await modelRuntime.refresh({ allowNetwork: false });
		const providerId = settingsManager.getDefaultProvider();
		const modelId = settingsManager.getDefaultModel();
		const model = providerId && modelId ? modelRuntime.getModel(providerId, modelId) : undefined;
		if (!model) {
			throw new Error("No Bittune Agent model is selected. Run `bittune configure` before starting a session.");
		}

		const resourceLoader = new DefaultResourceLoader({
			cwd,
			agentDir,
			settingsManager,
			extensionFactories: [
				{
					name: "bittune",
					factory: createBittuneExtension({ stateRoot: paths.state, namespaceId, initialCapabilities: restoredCapabilities, externalTools: mcpTools, closeExternalTools: closeMcpTools }),
					hidden: true,
				},
			],
			noExtensions: true,
			noSkills: true,
			noPromptTemplates: true,
			noThemes: true,
			noContextFiles: true,
			staticOnly: true,
			systemPrompt: BITTUNE_SYSTEM_PROMPT,
		});
		await resourceLoader.reload();

		const result = await createAgentSession({
			cwd,
			agentDir,
			modelRuntime,
			resourceLoader,
			settingsManager,
			sessionManager,
			model,
			// The static catalog is the trust boundary. `tools` is only its initial
			// Core projection plus capabilities restored from the persisted session.
			availableTools,
			tools: [...activeToolNamesForCapabilities(restoredCapabilities), ...mcpTools.map((tool) => tool.name)],
			...(sessionStartEvent ? { sessionStartEvent } : {}),
		});
		const services: AgentSessionServices = { cwd, agentDir, modelRuntime, settingsManager, resourceLoader, diagnostics };
		return {
			session: result.session,
			extensionsResult: result.extensionsResult,
			services,
			diagnostics,
			...(result.modelFallbackMessage ? { modelFallbackMessage: result.modelFallbackMessage } : {}),
		};
	};
}

export async function createBittuneRuntime(options: CreateBittuneRuntimeOptions = {}) {
	const cwd = resolve(options.cwd ?? process.cwd());
	const paths = resolveBittunePaths(options.paths);
	await ensureDirectories(paths);
	await assertBittuneAgentConfigurationInitialized(paths.agent);
	let sessionManager: SessionManager;
	if (options.sessionId) {
		assertValidSessionId(options.sessionId);
		const matches = (await readdir(paths.sessions)).filter((entry) => entry.endsWith(`_${options.sessionId}.jsonl`));
		if (matches.length !== 1) throw new Error(`Bittune session not found: ${options.sessionId}`);
		sessionManager = SessionManager.open(join(paths.sessions, matches[0]!), paths.sessions);
	} else {
		sessionManager = SessionManager.create(cwd, paths.sessions);
	}
	const runtime = await createAgentSessionRuntime(createRuntimeFactory(paths), {
		cwd,
		agentDir: paths.agent,
		sessionManager,
	});
	return { runtime, paths };
}

function printHelp(): void {
	console.log(`Bittune ${VERSION}

Usage:
  bittune [message]
  bittune configure --base-url <url> --model-id <id> [options]
  bittune mcp <list|get|test> [name]
  bittune doctor
  bittune version

Commands:
  configure   Configure the Bittune Agent LLM
  mcp         Inspect or test globally configured MCP servers
  doctor      Check Bittune runtime and local configuration
  version     Print the Bittune version

Options:
  --base-url <url>       OpenAI-compatible endpoint base URL
  --model-id <id>        Agent model ID
  --provider-id <id>     Provider ID (default: bittune-agent)
  --api-key-env <name>   Environment variable containing the API key
  --session <id>         Resume a persisted Bittune session
  --help                 Show this help`);
}

function printMcpHelp(): void {
	console.log(`Usage:
  bittune mcp list
  bittune mcp get <name>
  bittune mcp test [name]

MCP configuration is read only from $BITTUNE_HOME/mcp.json.`);
}

async function readJson(path: string): Promise<Record<string, unknown>> {
	try {
		return JSON.parse(await readFile(path, "utf8")) as Record<string, unknown>;
	} catch (error: unknown) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return {};
		throw error;
	}
}

async function configure(options: BittuneConfigureOptions): Promise<void> {
	const baseUrl = options.baseUrl?.trim();
	const modelId = options.modelId?.trim();
	const providerId = (options.providerId ?? "bittune-agent").trim();
	const apiKeyEnv = (options.apiKeyEnv ?? "BITTUNE_AGENT_LLM_API_KEY").trim();
	if (!baseUrl) throw new Error("--base-url is required.");
	let configuredBaseUrl: URL;
	try {
		configuredBaseUrl = new URL(baseUrl);
	} catch {
		throw new Error("--base-url must be an http(s) URL.");
	}
	if (!configuredBaseUrl.hostname || !["http:", "https:"].includes(configuredBaseUrl.protocol)) {
		throw new Error("--base-url must be an http(s) URL.");
	}
	if (!modelId) throw new Error("--model-id is required.");
	if (!/^[a-zA-Z][a-zA-Z0-9._-]{0,63}$/.test(providerId)) throw new Error("--provider-id is invalid.");
	if (!/^[A-Z][A-Z0-9_]*$/.test(apiKeyEnv)) throw new Error("--api-key-env must be an environment variable name.");

	const paths = resolveBittunePaths();
	await ensureDirectories(paths);
	const modelsPath = join(paths.agent, "models.json");
	const settingsPath = join(paths.agent, "settings.json");
	const hasCurrentConfiguration = await isBittuneAgentConfigurationInitialized(paths.agent);
	const config = hasCurrentConfiguration ? await readJson(modelsPath) : {};
	const providers =
		config.providers && typeof config.providers === "object" && !Array.isArray(config.providers)
			? (config.providers as Record<string, unknown>)
			: {};
	providers[providerId] = {
		baseUrl: configuredBaseUrl.toString().replace(/\/$/, ""),
		api: "openai-completions",
		apiKey: `$${apiKeyEnv}`,
		authHeader: true,
		models: [{ id: modelId, name: "Bittune Agent Model", input: ["text"], reasoning: false }],
	};
	await writeFile(modelsPath, `${JSON.stringify({ ...config, providers }, null, 2)}\n`, { mode: 0o600 });
	await chmod(modelsPath, 0o600).catch(() => undefined);
	const settings = hasCurrentConfiguration ? await readJson(settingsPath) : {};
	await writeFile(
		settingsPath,
		`${JSON.stringify({ ...settings, defaultProvider: providerId, defaultModel: modelId }, null, 2)}\n`,
		{ mode: 0o600 },
	);
	await chmod(settingsPath, 0o600).catch(() => undefined);
	await writeBittuneAgentConfigurationMarker(paths.agent);
	console.log(`Bittune Agent model configured. Export ${apiKeyEnv} in the current shell before running bittune.`);
}

async function doctor(): Promise<void> {
	const paths = resolveBittunePaths();
	await ensureDirectories(paths);
	if (!(await isBittuneAgentConfigurationInitialized(paths.agent))) {
		console.log(`Bittune ${VERSION}`);
		console.log(`Agent directory: ${paths.agent}`);
		console.log("Agent configuration: not initialized");
		console.log("Run `bittune configure --base-url <url> --model-id <id>`; previous Agent configuration is not imported.");
		process.exitCode = 1;
		return;
	}
	const settingsManager = SettingsManager.create(process.cwd(), paths.agent, { projectTrusted: false });
	const modelRuntime = await ModelRuntime.create({
		authPath: join(paths.agent, "auth.json"),
		modelsPath: join(paths.agent, "models.json"),
		refreshOnCreate: false,
		allowModelNetwork: false,
	});
	await modelRuntime.refresh({ allowNetwork: false });
	const providerId = settingsManager.getDefaultProvider();
	const modelId = settingsManager.getDefaultModel();
	const model = providerId && modelId ? modelRuntime.getModel(providerId, modelId) : undefined;
	console.log(`Bittune ${VERSION}`);
	console.log(`Agent directory: ${paths.agent}`);
	console.log(`State directory: ${paths.state}`);
	console.log(`Agent model selection: ${model && providerId ? `${providerId}/${model.id}` : "missing"}`);
	console.log(`Agent authentication: ${model && modelRuntime.hasConfiguredAuth(model.provider) ? "available" : "missing"}`);
	if (!model || !modelRuntime.hasConfiguredAuth(model.provider)) process.exitCode = 1;
}

async function runMcpCommand(command: BittuneMcpCommand): Promise<void> {
	if (command.kind === "help") {
		printMcpHelp();
		return;
	}
	const paths = resolveBittunePaths();
	await ensureDirectories(paths);
	const configuration = await loadMcpConfiguration(join(paths.root, "mcp.json"));
	if (command.kind === "list") {
		if (configuration.servers.length === 0) {
			console.log("No MCP servers are configured.");
			return;
		}
		for (const server of configuration.servers) {
			console.log(`${server.name}: ${server.enabled ? "enabled" : "disabled"} (${server.transport}, ${server.effect})`);
			console.log(`  URL: ${server.url}`);
			console.log(`  Allowed tools: ${server.allowTools.join(", ")}`);
		}
		return;
	}
	if (command.kind === "get") {
		const server = configuration.servers.find((item) => item.name === command.name);
		if (!server) throw new Error(`MCP server "${command.name}" is not configured.`);
		console.log(JSON.stringify(publicMcpServerConfig(server), null, 2));
		return;
	}

	const selected = command.name
		? configuration.servers.filter((server) => server.name === command.name)
		: configuration.servers;
	if (selected.length === 0) throw new Error(`MCP server "${command.name}" is not configured.`);
	const enabled = selected.filter((server) => server.enabled);
	for (const server of selected.filter((server) => !server.enabled)) console.log(`${server.name}: disabled; skipped.`);
	if (enabled.length === 0) return;
	const result = await discoverConfiguredMcpTools({ servers: enabled });
	try {
		for (const server of result.servers) {
			console.log(`${server.serverName}: connected`);
			console.log(`  Discovered tools: ${server.discoveredToolNames.length ? server.discoveredToolNames.join(", ") : "none"}`);
			console.log(`  Registered tools: ${server.tools.length ? server.tools.map((tool) => tool.remoteName).join(", ") : "none"}`);
			if (server.missingAllowedToolNames.length > 0) console.log(`  Missing allowed tools: ${server.missingAllowedToolNames.join(", ")}`);
		}
		for (const diagnostic of result.diagnostics) {
			console.error(`${diagnostic.serverName}: ${diagnostic.code}: ${diagnostic.message}`);
			process.exitCode = 1;
		}
	} finally {
		await result.close();
	}
}

export async function runBittuneCli(argv: string[]): Promise<void> {
	const invocation = parseBittuneCliArguments(argv);
	if (invocation.kind === "help") {
		printHelp();
		return;
	}
	if (invocation.kind === "version") {
		console.log(`Bittune ${VERSION}`);
		return;
	}
	if (invocation.kind === "configure") {
		await configure(invocation.options);
		return;
	}
	if (invocation.kind === "mcp") {
		await runMcpCommand(invocation.command);
		return;
	}
	if (invocation.kind === "doctor") {
		await doctor();
		return;
	}

	if (invocation.sessionId) assertValidSessionId(invocation.sessionId);
	const { runtime } = await createBittuneRuntime(invocation.sessionId ? { sessionId: invocation.sessionId } : undefined);
	const mode = new InteractiveMode(runtime, invocation.message ? { initialMessage: invocation.message } : undefined);
	await mode.run();
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
	await runBittuneCli(process.argv.slice(2)).catch((error: unknown) => {
		const message = error instanceof Error ? error.message : String(error);
		console.error(`bittune: ${message}`);
		process.exitCode = 1;
	});
}
