import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile, mkdir, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer } from "node:http";
import test from "node:test";
import { createBittuneRuntime, runBittuneCli } from "../packages/bittune-runtime/src/bittune.ts";
import { parseBittuneCliArguments } from "../packages/bittune-runtime/src/cli/bittune-args.ts";
import { formatNoApiKeyFoundMessage, formatNoModelSelectedMessage } from "../packages/bittune-runtime/src/core/auth-guidance.ts";
import { BITTUNE_UNAVAILABLE_SLASH_COMMANDS, isBittuneUnavailableSlashCommand } from "../packages/bittune-runtime/src/core/bittune-product-policy.ts";
import { BUILTIN_SLASH_COMMANDS } from "../packages/bittune-runtime/src/core/slash-commands.ts";
import { formatResumeCommand } from "../packages/bittune-runtime/src/modes/interactive/interactive-mode.ts";
import { CAPABILITY_ACTIVATION_TOOL, CAPABILITY_TOOL_NAMES, CORE_TOOL_NAMES } from "../packages/bittune-capabilities/src/shared/capability-catalog.ts";
import { createToolRegistry } from "../packages/bittune-capabilities/src/tool-registry.ts";

async function markBittuneAgentConfiguration(agent: string): Promise<void> {
	await writeFile(join(agent, "bittune-runtime.json"), `${JSON.stringify({ schema_version: 1 })}\n`);
}

test("Bittune CLI rejects unsupported and malformed options before starting a session", () => {
	assert.deepEqual(parseBittuneCliArguments([]), {
		kind: "interactive",
	});
	assert.throws(() => parseBittuneCliArguments(["--session"]), /--session.*requires a value/i);
	assert.throws(() => parseBittuneCliArguments(["--session", "one", "--session", "two"]), /may only be specified once/i);
	assert.throws(() => parseBittuneCliArguments(["--session-dir", "/tmp/session"]), /Unknown option/i);
	assert.throws(() => parseBittuneCliArguments(["--unknown"]), /Unknown option/i);
	assert.throws(() => parseBittuneCliArguments(["configure", "--base-url", "http://example.test", "--unknown", "x"]), /Unknown option/i);
	assert.throws(() => parseBittuneCliArguments(["configure", "--base-url", "--model-id", "agent"]), /--base-url.*requires a value/i);
	assert.throws(() => parseBittuneCliArguments(["doctor", "unexpected"]), /does not accept arguments/i);
	assert.deepEqual(parseBittuneCliArguments(["describe", "the", "host"]), {
		kind: "interactive",
		message: "describe the host",
	});
	assert.deepEqual(parseBittuneCliArguments(["--session", "session-123"]), {
		kind: "interactive",
		sessionId: "session-123",
	});
});

test("Bittune validates a resume session ID before loading Runtime configuration", async () => {
	await assert.rejects(runBittuneCli(["--session", "../not-a-session"]), /session id must be non-empty/i);
});

test("Bittune exposes only configure-based authentication and static model selection", () => {
	const guidance = `${formatNoApiKeyFoundMessage("bittune-agent")}\n${formatNoModelSelectedMessage()}`;
	assert.match(guidance, /bittune configure/);
	assert.match(guidance, /bittune doctor/);
	assert.doesNotMatch(guidance, /\/login|\/logout|\/model|oauth|pi-coding-agent/i);
	assert.equal(BUILTIN_SLASH_COMMANDS.some((command) => command.name === "model"), false);
	for (const command of ["/model", "/model provider/model", "/login", "/logout", "/logout provider"]) {
		assert.equal(isBittuneUnavailableSlashCommand(command), true);
	}
	assert.deepEqual(BITTUNE_UNAVAILABLE_SLASH_COMMANDS.slice(7, 10), ["/model", "/login", "/logout"]);
});

test("Bittune Runtime starts with Core Tools and retains the static catalog for later activation", async () => {
	const root = await mkdtemp(join(tmpdir(), "bittune-runtime-"));
	const agent = join(root, "agent");
	const sessions = join(agent, "sessions");
	const state = join(root, "state");
	const logs = join(root, "logs");
	const previousKey = process.env.BITTUNE_TEST_API_KEY;
	process.env.BITTUNE_TEST_API_KEY = "test-key";
	try {
		await mkdir(join(root, ".pi"), { recursive: true });
		await mkdir(agent, { recursive: true });
		await writeFile(
			join(agent, "models.json"),
			JSON.stringify({
				providers: {
					"bittune-test": {
						baseUrl: "http://127.0.0.1:18000/v1",
						api: "openai-completions",
						apiKey: "$BITTUNE_TEST_API_KEY",
						authHeader: true,
						models: [{ id: "test-agent-model", input: ["text"], reasoning: false }],
					},
				},
			}),
		);
		await writeFile(
			join(agent, "settings.json"),
			JSON.stringify({ defaultProvider: "bittune-test", defaultModel: "test-agent-model" }),
		);
		await markBittuneAgentConfiguration(agent);

		const { runtime, paths } = await createBittuneRuntime({
			cwd: root,
			paths: { root, agent, sessions, state, logs },
		});
		try {
			assert.deepEqual(paths, { root, agent, sessions, state, logs });
			const toolNames: string[] = runtime.session.agent.state.tools.map((tool) => tool.name);
			const toolNameSet: Set<string> = new Set(toolNames);
			assert.deepEqual(toolNames, [...CORE_TOOL_NAMES]);
			assert.equal(toolNameSet.has("list_deployment_presets"), false);
			assert.equal(runtime.session.getAllTools().some((tool) => tool.name === "list_deployment_presets"), true);
			assert.equal(runtime.session.getAllTools().some((tool) => tool.name === "edit"), false);
			assert.equal(runtime.session.getAllTools().some((tool) => tool.name === CAPABILITY_ACTIVATION_TOOL), true);
			assert.equal(runtime.session.getAllTools().filter((tool) => tool.name === "run_performance_test").length, 1);
			assert.equal(createToolRegistry(state).length, 44);
			assert.deepEqual(runtime.session.resourceLoader.getAgentsFiles().agentsFiles, []);
			assert.deepEqual(runtime.session.resourceLoader.getSkills().skills, []);
			assert.deepEqual(runtime.session.resourceLoader.getPrompts().prompts, []);
			assert.equal(runtime.session.resourceLoader.getExtensions().extensions.length, 1);
			assert.equal(runtime.session.agent.state.model?.provider, "bittune-test");
		} finally {
			await runtime.dispose();
		}
	} finally {
		if (previousKey === undefined) delete process.env.BITTUNE_TEST_API_KEY;
		else process.env.BITTUNE_TEST_API_KEY = previousKey;
		await rm(root, { recursive: true, force: true });
	}
});

test("Bittune Runtime never imports an unmarked previous Agent configuration", async () => {
	const root = await mkdtemp(join(tmpdir(), "bittune-uninitialized-agent-"));
	const agent = join(root, "agent");
	try {
		await mkdir(agent, { recursive: true });
		await writeFile(
			join(agent, "models.json"),
			JSON.stringify({ providers: { legacy: { baseUrl: "http://127.0.0.1:1/v1" } } }),
		);
		await assert.rejects(
			() => createBittuneRuntime({ cwd: root, paths: { root, agent } }),
			/not initialized for this Runtime/,
		);
	} finally {
		await rm(root, { recursive: true, force: true });
	}
});

test("first Bittune configure replaces an unmarked previous Agent configuration", async () => {
	const root = await mkdtemp(join(tmpdir(), "bittune-configure-clean-break-"));
	const agent = join(root, "agent");
	const previousHome = process.env.BITTUNE_HOME;
	process.env.BITTUNE_HOME = root;
	try {
		await mkdir(agent, { recursive: true });
		await writeFile(
			join(agent, "models.json"),
			JSON.stringify({ providers: { legacy: { baseUrl: "http://127.0.0.1:1/v1" } } }),
		);
		await writeFile(join(agent, "settings.json"), JSON.stringify({ defaultProvider: "legacy", defaultModel: "old-model" }));
		await runBittuneCli([
			"configure",
			"--base-url",
			"http://127.0.0.1:18000/v1",
			"--model-id",
			"new-agent-model",
		]);
		const models = JSON.parse(await readFile(join(agent, "models.json"), "utf8")) as { providers: Record<string, unknown> };
		const settings = JSON.parse(await readFile(join(agent, "settings.json"), "utf8")) as Record<string, unknown>;
		const marker = JSON.parse(await readFile(join(agent, "bittune-runtime.json"), "utf8")) as Record<string, unknown>;
		assert.deepEqual(Object.keys(models.providers), ["bittune-agent"]);
		assert.deepEqual(settings, { defaultProvider: "bittune-agent", defaultModel: "new-agent-model" });
		assert.equal(marker.schema_version, 1);
	} finally {
		if (previousHome === undefined) delete process.env.BITTUNE_HOME;
		else process.env.BITTUNE_HOME = previousHome;
		await rm(root, { recursive: true, force: true });
	}
});

test("Bittune Runtime preserves Pi Agent Loop streaming and final assistant messages", async () => {
	const requests: string[] = [];
	const server = createServer((request, response) => {
		let body = "";
		request.on("data", (chunk) => {
			body += chunk.toString();
		});
		request.on("end", () => {
			requests.push(body);
			response.writeHead(200, {
				"content-type": "text/event-stream",
				connection: "keep-alive",
				"cache-control": "no-cache",
			});
			response.write('data: {"id":"bittune-test","object":"chat.completion.chunk","created":1,"model":"test-agent-model","choices":[{"index":0,"delta":{"role":"assistant","content":"Bittune"},"finish_reason":null}]}\n\n');
			response.write('data: {"id":"bittune-test","object":"chat.completion.chunk","created":1,"model":"test-agent-model","choices":[{"index":0,"delta":{"content":" is ready"},"finish_reason":null}]}\n\n');
			response.write('data: {"id":"bittune-test","object":"chat.completion.chunk","created":1,"model":"test-agent-model","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n');
			response.end("data: [DONE]\n\n");
		});
	});
	await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
	const address = server.address();
	assert.equal(typeof address, "object");
	const port = (address as { port: number }).port;
	const root = await mkdtemp(join(tmpdir(), "bittune-agent-loop-"));
	const agent = join(root, "agent");
	try {
		await mkdir(agent, { recursive: true });
		await writeFile(
			join(agent, "models.json"),
			JSON.stringify({
				providers: {
					"bittune-test": {
						baseUrl: `http://127.0.0.1:${port}/v1`,
						api: "openai-completions",
						apiKey: "$BITTUNE_TEST_API_KEY",
						authHeader: true,
						models: [{ id: "test-agent-model", input: ["text"], reasoning: false }],
					},
				},
			}),
		);
		await writeFile(
			join(agent, "settings.json"),
			JSON.stringify({ defaultProvider: "bittune-test", defaultModel: "test-agent-model" }),
		);
		await markBittuneAgentConfiguration(agent);
		const previousKey = process.env.BITTUNE_TEST_API_KEY;
		process.env.BITTUNE_TEST_API_KEY = "test-key";
		const { runtime } = await createBittuneRuntime({ cwd: root, paths: { root, agent } });
		try {
			await runtime.session.prompt("hello");
			const assistant = runtime.session.state.messages.find((message) => message.role === "assistant");
			assert.ok(assistant);
			assert.equal(
				Array.isArray(assistant.content)
					? assistant.content.filter((part) => part.type === "text").map((part) => part.text).join("")
					: assistant.content,
				"Bittune is ready",
			);
			assert.equal(requests.length, 1);
			assert.match(requests[0] ?? "", /bittune-system-prompt:v2/);
			assert.doesNotMatch(requests[0] ?? "", /expert coding assistant operating inside pi/i);
		} finally {
			await runtime.dispose();
			if (previousKey === undefined) delete process.env.BITTUNE_TEST_API_KEY;
			else process.env.BITTUNE_TEST_API_KEY = previousKey;
		}
	} finally {
		await rm(root, { recursive: true, force: true });
		await new Promise<void>((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
	}
});

test("Bittune Runtime activates a Capability before calling its Domain Tool", async () => {
	const requests: Array<Record<string, unknown>> = [];
	const server = createServer((request, response) => {
		let body = "";
		request.on("data", (chunk) => {
			body += chunk.toString();
		});
		request.on("end", () => {
			const payload = JSON.parse(body) as Record<string, unknown>;
			requests.push(payload);
			response.writeHead(200, {
				"content-type": "text/event-stream",
				connection: "keep-alive",
				"cache-control": "no-cache",
			});
			if (requests.length === 1) {
				response.write('data: {"id":"bittune-tool-call","object":"chat.completion.chunk","created":1,"model":"test-agent-model","choices":[{"index":0,"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call-activate-deployment","type":"function","function":{"name":"activate_capability","arguments":"{\\"capability\\":\\"deployment\\"}"}}]},"finish_reason":null}]}\n\n');
				response.write('data: {"id":"bittune-tool-call","object":"chat.completion.chunk","created":1,"model":"test-agent-model","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n');
			} else if (requests.length === 2) {
				response.write('data: {"id":"bittune-tool-call","object":"chat.completion.chunk","created":1,"model":"test-agent-model","choices":[{"index":0,"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call-list-presets","type":"function","function":{"name":"list_deployment_presets","arguments":"{}"}}]},"finish_reason":null}]}\n\n');
				response.write('data: {"id":"bittune-tool-call","object":"chat.completion.chunk","created":1,"model":"test-agent-model","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n');
			} else {
				response.write('data: {"id":"bittune-tool-result","object":"chat.completion.chunk","created":1,"model":"test-agent-model","choices":[{"index":0,"delta":{"role":"assistant","content":"Tool observation received."},"finish_reason":null}]}\n\n');
				response.write('data: {"id":"bittune-tool-result","object":"chat.completion.chunk","created":1,"model":"test-agent-model","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n');
			}
			response.end("data: [DONE]\n\n");
		});
	});
	await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
	const address = server.address();
	assert.equal(typeof address, "object");
	const port = (address as { port: number }).port;
	const root = await mkdtemp(join(tmpdir(), "bittune-tool-loop-"));
	const agent = join(root, "agent");
	const previousKey = process.env.BITTUNE_TEST_API_KEY;
	process.env.BITTUNE_TEST_API_KEY = "test-key";
	try {
		await mkdir(agent, { recursive: true });
		await writeFile(
			join(agent, "models.json"),
			JSON.stringify({
				providers: {
					"bittune-test": {
						baseUrl: `http://127.0.0.1:${port}/v1`,
						api: "openai-completions",
						apiKey: "$BITTUNE_TEST_API_KEY",
						authHeader: true,
						models: [{ id: "test-agent-model", input: ["text"], reasoning: false }],
					},
				},
			}),
		);
		await writeFile(
			join(agent, "settings.json"),
			JSON.stringify({ defaultProvider: "bittune-test", defaultModel: "test-agent-model" }),
		);
		await markBittuneAgentConfiguration(agent);
		const { runtime } = await createBittuneRuntime({ cwd: root, paths: { root, agent } });
		try {
			await runtime.session.prompt("list available deployment presets");
			assert.equal(requests.length, 3);
			const firstRequest = JSON.stringify(requests[0]);
			assert.match(firstRequest, /"activate_capability"/);
			assert.doesNotMatch(firstRequest, /"list_deployment_presets"/);
			const secondRequest = JSON.stringify(requests[1]);
			assert.match(secondRequest, /"role":"tool"/);
			assert.match(secondRequest, /"tool_call_id":"call-activate-deployment"/);
			assert.match(secondRequest, /"list_deployment_presets"/);
			assert.match(secondRequest, /"derive_deployment_options"/);
			assert.equal(CAPABILITY_TOOL_NAMES.deployment.includes("list_deployment_presets"), true);
			const thirdRequest = JSON.stringify(requests[2]);
			assert.match(thirdRequest, /"tool_call_id":"call-list-presets"/);
			const finalAssistant = runtime.session.state.messages.filter((message) => message.role === "assistant").at(-1);
			assert.ok(finalAssistant);
			assert.equal(
				Array.isArray(finalAssistant.content)
					? finalAssistant.content.filter((part) => part.type === "text").map((part) => part.text).join("")
					: finalAssistant.content,
				"Tool observation received.",
			);
		} finally {
			await runtime.dispose();
		}
	} finally {
		if (previousKey === undefined) delete process.env.BITTUNE_TEST_API_KEY;
		else process.env.BITTUNE_TEST_API_KEY = previousKey;
		await rm(root, { recursive: true, force: true });
		await new Promise<void>((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
	}
});

test("Bittune Runtime preserves provider failures and empty stream errors", async () => {
	let mode: "auth" | "rate" | "server" | "empty" = "auth";
	const server = createServer((request, response) => {
		request.resume();
		request.on("end", () => {
			if (mode === "empty") {
				response.writeHead(200, { "content-type": "text/event-stream" });
				response.end("data: [DONE]\n\n");
				return;
			}
			const status = mode === "auth" ? 401 : mode === "rate" ? 429 : 500;
			response.writeHead(status, { "content-type": "application/json" });
			response.end(JSON.stringify({ error: { message: `${mode} failure` } }));
		});
	});
	await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
	const address = server.address();
	assert.equal(typeof address, "object");
	const port = (address as { port: number }).port;
	const root = await mkdtemp(join(tmpdir(), "bittune-provider-errors-"));
	const agent = join(root, "agent");
	const previousKey = process.env.BITTUNE_TEST_API_KEY;
	process.env.BITTUNE_TEST_API_KEY = "test-key";
	try {
		await mkdir(agent, { recursive: true });
		await writeFile(
			join(agent, "models.json"),
			JSON.stringify({
				providers: {
					"bittune-test": {
						baseUrl: `http://127.0.0.1:${port}/v1`,
						api: "openai-completions",
						apiKey: "$BITTUNE_TEST_API_KEY",
						authHeader: true,
						models: [{ id: "test-agent-model", input: ["text"], reasoning: false }],
					},
				},
			}),
		);
		await writeFile(
			join(agent, "settings.json"),
			JSON.stringify({ defaultProvider: "bittune-test", defaultModel: "test-agent-model", retry: { enabled: false } }),
		);
		await markBittuneAgentConfiguration(agent);
		const { runtime } = await createBittuneRuntime({ cwd: root, paths: { root, agent } });
		try {
			await runtime.session.prompt("trigger auth error");
			let assistant = runtime.session.state.messages.filter((message) => message.role === "assistant").at(-1);
			assert.ok(assistant);
			assert.equal(assistant.stopReason, "error");
			assert.match(assistant.errorMessage ?? "", /401|auth failure/i);

			mode = "rate";
			await runtime.session.prompt("trigger rate error");
			assistant = runtime.session.state.messages.filter((message) => message.role === "assistant").at(-1);
			assert.ok(assistant);
			assert.equal(assistant.stopReason, "error");
			assert.match(assistant.errorMessage ?? "", /429|rate failure/i);

			mode = "server";
			await runtime.session.prompt("trigger server error");
			assistant = runtime.session.state.messages.filter((message) => message.role === "assistant").at(-1);
			assert.ok(assistant);
			assert.equal(assistant.stopReason, "error");
			assert.match(assistant.errorMessage ?? "", /500|server failure/i);

			mode = "empty";
			await runtime.session.prompt("trigger empty stream");
			assistant = runtime.session.state.messages.filter((message) => message.role === "assistant").at(-1);
			assert.ok(assistant);
			assert.equal(assistant.stopReason, "error");
			assert.match(assistant.errorMessage ?? "", /finish_reason/i);
		} finally {
			await runtime.dispose();
		}
	} finally {
		if (previousKey === undefined) delete process.env.BITTUNE_TEST_API_KEY;
		else process.env.BITTUNE_TEST_API_KEY = previousKey;
		await rm(root, { recursive: true, force: true });
		await new Promise<void>((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
	}
});

test("Bittune Runtime retries a transient rate limit and then completes", async () => {
	let requests = 0;
	const server = createServer((request, response) => {
		request.resume();
		request.on("end", () => {
			requests++;
			if (requests === 1) {
				response.writeHead(429, { "content-type": "application/json" });
				response.end(JSON.stringify({ error: { message: "temporary rate limit" } }));
				return;
			}
			response.writeHead(200, { "content-type": "text/event-stream" });
			response.write('data: {"id":"bittune-retry","object":"chat.completion.chunk","created":1,"model":"test-agent-model","choices":[{"index":0,"delta":{"role":"assistant","content":"retry succeeded"},"finish_reason":null}]}\n\n');
			response.write('data: {"id":"bittune-retry","object":"chat.completion.chunk","created":1,"model":"test-agent-model","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n');
			response.end("data: [DONE]\n\n");
		});
	});
	await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
	const address = server.address();
	assert.equal(typeof address, "object");
	const port = (address as { port: number }).port;
	const root = await mkdtemp(join(tmpdir(), "bittune-retry-"));
	const agent = join(root, "agent");
	const previousKey = process.env.BITTUNE_TEST_API_KEY;
	process.env.BITTUNE_TEST_API_KEY = "test-key";
	try {
		await mkdir(agent, { recursive: true });
		await writeFile(
			join(agent, "models.json"),
			JSON.stringify({
				providers: {
					"bittune-test": {
						baseUrl: `http://127.0.0.1:${port}/v1`,
						api: "openai-completions",
						apiKey: "$BITTUNE_TEST_API_KEY",
						authHeader: true,
						models: [{ id: "test-agent-model", input: ["text"], reasoning: false }],
					},
				},
			}),
		);
		await writeFile(
			join(agent, "settings.json"),
			JSON.stringify({
				defaultProvider: "bittune-test",
				defaultModel: "test-agent-model",
				retry: { enabled: true, maxRetries: 1, baseDelayMs: 1 },
			}),
		);
		await markBittuneAgentConfiguration(agent);
		const { runtime } = await createBittuneRuntime({ cwd: root, paths: { root, agent } });
		try {
			await runtime.session.prompt("retry the transient provider failure");
			assert.equal(requests, 2);
			const finalAssistant = runtime.session.state.messages.filter((message) => message.role === "assistant").at(-1);
			assert.ok(finalAssistant);
			assert.equal(finalAssistant.stopReason, "stop");
			assert.equal(
				Array.isArray(finalAssistant.content)
					? finalAssistant.content.filter((part) => part.type === "text").map((part) => part.text).join("")
					: finalAssistant.content,
				"retry succeeded",
			);
		} finally {
			await runtime.dispose();
		}
	} finally {
		if (previousKey === undefined) delete process.env.BITTUNE_TEST_API_KEY;
		else process.env.BITTUNE_TEST_API_KEY = previousKey;
		await rm(root, { recursive: true, force: true });
		await new Promise<void>((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
	}
});

test("Bittune Runtime restores a persisted session through the Bittune resume command", async () => {
	const server = createServer((request, response) => {
		request.resume();
		request.on("end", () => {
			response.writeHead(200, {
				"content-type": "text/event-stream",
				connection: "keep-alive",
				"cache-control": "no-cache",
			});
			response.write('data: {"id":"bittune-test","object":"chat.completion.chunk","created":1,"model":"test-agent-model","choices":[{"index":0,"delta":{"role":"assistant","content":"persisted response"},"finish_reason":null}]}\n\n');
			response.write('data: {"id":"bittune-test","object":"chat.completion.chunk","created":1,"model":"test-agent-model","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n');
			response.end("data: [DONE]\n\n");
		});
	});
	await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
	const address = server.address();
	assert.equal(typeof address, "object");
	const port = (address as { port: number }).port;
	const root = await mkdtemp(join(tmpdir(), "bittune-session-resume-"));
	const agent = join(root, "agent");
	const sessions = join(agent, "sessions");
	const previousKey = process.env.BITTUNE_TEST_API_KEY;
	const stdoutDescriptor = Object.getOwnPropertyDescriptor(process.stdout, "isTTY");
	process.env.BITTUNE_TEST_API_KEY = "test-key";
	try {
		await mkdir(agent, { recursive: true });
		await writeFile(
			join(agent, "models.json"),
			JSON.stringify({
				providers: {
					"bittune-test": {
						baseUrl: `http://127.0.0.1:${port}/v1`,
						api: "openai-completions",
						apiKey: "$BITTUNE_TEST_API_KEY",
						authHeader: true,
						models: [{ id: "test-agent-model", input: ["text"], reasoning: false }],
					},
				},
			}),
		);
		await writeFile(
			join(agent, "settings.json"),
			JSON.stringify({ defaultProvider: "bittune-test", defaultModel: "test-agent-model" }),
		);
		await markBittuneAgentConfiguration(agent);

		const paths = { root, agent, sessions };
		const { runtime: initialRuntime } = await createBittuneRuntime({ cwd: root, paths });
		let sessionId: string;
		try {
			await initialRuntime.session.prompt("persist this turn");
			const activation = initialRuntime.session.agent.state.tools.find((tool) => tool.name === CAPABILITY_ACTIVATION_TOOL);
			assert.ok(activation);
			await activation.execute("persist-deployment-capability", { capability: "deployment" });
			assert.equal(initialRuntime.session.getActiveToolNames().includes("list_deployment_presets"), true);
			sessionId = initialRuntime.session.sessionManager.getSessionId();
			Object.defineProperty(process.stdout, "isTTY", { configurable: true, value: true });
			const resumeCommand = formatResumeCommand(initialRuntime.session.sessionManager);
			assert.equal(resumeCommand, `bittune --session ${sessionId}`);
		} finally {
			await initialRuntime.dispose();
		}

		const { runtime: resumedRuntime } = await createBittuneRuntime({ cwd: root, paths, sessionId: sessionId! });
		try {
			assert.equal(resumedRuntime.session.sessionManager.getSessionId(), sessionId!);
			assert.equal(resumedRuntime.session.getActiveToolNames().includes("list_deployment_presets"), true);
			const restoredAssistant = resumedRuntime.session.state.messages.find((message) => message.role === "assistant");
			assert.ok(restoredAssistant);
			assert.equal(
				Array.isArray(restoredAssistant.content)
					? restoredAssistant.content.filter((part) => part.type === "text").map((part) => part.text).join("")
					: restoredAssistant.content,
				"persisted response",
			);
		} finally {
			await resumedRuntime.dispose();
		}
	} finally {
		if (stdoutDescriptor) Object.defineProperty(process.stdout, "isTTY", stdoutDescriptor);
		else Reflect.deleteProperty(process.stdout, "isTTY");
		if (previousKey === undefined) delete process.env.BITTUNE_TEST_API_KEY;
		else process.env.BITTUNE_TEST_API_KEY = previousKey;
		await rm(root, { recursive: true, force: true });
		await new Promise<void>((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
	}
});
