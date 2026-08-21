import assert from "node:assert/strict";
import { execFileSync, spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createServer, type Server } from "node:http";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { tmpdir } from "node:os";
import test from "node:test";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const hasLinuxScript = (() => {
	if (process.platform !== "linux") return false;
	try {
		execFileSync("script", ["--version"], { stdio: "ignore" });
		return true;
	} catch {
		return false;
	}
})();

function shellQuote(value: string): string {
	return `'${value.replaceAll("'", "'\\''")}'`;
}

function stripTerminalControl(text: string): string {
	return text
		.replace(/\u001b\][^\u0007]*(?:\u0007|\u001b\\)/g, "")
		.replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, "")
		.replace(/\r/g, "");
}

type PtyRun = {
	child: ChildProcessWithoutNullStreams;
	getOutput: () => string;
	waitForOutput: (predicate: (output: string) => boolean, timeoutMs?: number) => Promise<string>;
	waitForExit: () => Promise<{ code: number | null; output: string }>;
	write: (input: string) => void;
	terminate: () => void;
};

function spawnBittunePty(args: string[], env: NodeJS.ProcessEnv): PtyRun {
	const command = [
		process.execPath,
		"--import",
		"tsx",
		join(repoRoot, "packages/bittune-runtime/src/bittune.ts"),
		...args,
	]
		.map(shellQuote)
		.join(" ");
	const child = spawn("script", ["-qefc", command, "/dev/null"], {
		cwd: repoRoot,
		env,
		stdio: ["pipe", "pipe", "pipe"],
	});
	let output = "";
	const append = (chunk: Buffer): void => {
		output += chunk.toString("utf8");
	};
	child.stdout.on("data", append);
	child.stderr.on("data", append);

	return {
		child,
		getOutput: () => output,
		waitForOutput: async (predicate, timeoutMs = 15_000) => {
			const startedAt = Date.now();
			while (!predicate(stripTerminalControl(output))) {
				if (child.exitCode !== null) {
					throw new Error(`Bittune PTY exited before expected output (code ${child.exitCode}):\n${stripTerminalControl(output)}`);
				}
				if (Date.now() - startedAt >= timeoutMs) {
					throw new Error(`Timed out waiting for Bittune PTY output:\n${stripTerminalControl(output)}`);
				}
				await new Promise((resolve) => setTimeout(resolve, 25));
			}
			return stripTerminalControl(output);
		},
		waitForExit: () =>
			new Promise((resolve, reject) => {
				child.once("error", reject);
				child.once("close", (code) => resolve({ code, output: stripTerminalControl(output) }));
			}),
		write: (input) => child.stdin.write(input),
		terminate: () => {
			if (child.exitCode === null) child.kill("SIGTERM");
		},
	};
}

async function configureTestAgent(root: string, port: number): Promise<void> {
	const agent = join(root, "agent");
	await mkdir(agent, { recursive: true });
	await writeFile(
		join(agent, "models.json"),
		JSON.stringify({
			providers: {
				"bittune-pty": {
					baseUrl: `http://127.0.0.1:${port}/v1`,
					api: "openai-completions",
					apiKey: "$BITTUNE_PTY_API_KEY",
					authHeader: true,
					models: [{ id: "pty-agent-model", input: ["text"], reasoning: false }],
				},
			},
		}),
	);
	await writeFile(
		join(agent, "settings.json"),
		JSON.stringify({ defaultProvider: "bittune-pty", defaultModel: "pty-agent-model" }),
	);
	await writeFile(join(agent, "bittune-runtime.json"), `${JSON.stringify({ schema_version: 1 })}\n`);
}

function listen(server: Server): Promise<number> {
	return new Promise((resolve, reject) => {
		server.once("error", reject);
		server.listen(0, "127.0.0.1", () => {
			const address = server.address();
			if (!address || typeof address === "string") {
				reject(new Error("PTY test server did not expose a TCP port"));
				return;
			}
			resolve(address.port);
		});
	});
}

function closeServer(server: Server): Promise<void> {
	return new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
}

test("Bittune TUI renders a Tool Call/Result and executes its printed session resume command", { skip: !hasLinuxScript }, async () => {
	let requests = 0;
	const server = createServer((request, response) => {
		request.resume();
		request.on("end", () => {
			requests += 1;
			response.writeHead(200, {
				"content-type": "text/event-stream",
				connection: "keep-alive",
				"cache-control": "no-cache",
			});
			if (requests === 1) {
				response.write(
					'data: {"id":"pty-tool-call","object":"chat.completion.chunk","created":1,"model":"pty-agent-model","choices":[{"index":0,"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call-activate-deployment","type":"function","function":{"name":"activate_capability","arguments":"{\\"capability\\":\\"deployment\\"}"}}]},"finish_reason":null}]}\n\n',
				);
				response.write(
					'data: {"id":"pty-tool-call","object":"chat.completion.chunk","created":1,"model":"pty-agent-model","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n',
				);
			} else if (requests === 2) {
				response.write(
					'data: {"id":"pty-tool-call","object":"chat.completion.chunk","created":1,"model":"pty-agent-model","choices":[{"index":0,"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call-list-presets","type":"function","function":{"name":"list_deployment_presets","arguments":"{}"}}]},"finish_reason":null}]}\n\n',
				);
				response.write(
					'data: {"id":"pty-tool-call","object":"chat.completion.chunk","created":1,"model":"pty-agent-model","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n',
				);
			} else {
				response.write(
					'data: {"id":"pty-tool-result","object":"chat.completion.chunk","created":1,"model":"pty-agent-model","choices":[{"index":0,"delta":{"role":"assistant","content":"PTY Tool Call continuation passed."},"finish_reason":null}]}\n\n',
				);
				response.write(
					'data: {"id":"pty-tool-result","object":"chat.completion.chunk","created":1,"model":"pty-agent-model","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
				);
			}
			response.end("data: [DONE]\n\n");
		});
	});
	const port = await listen(server);
	const root = await mkdtemp(join(tmpdir(), "bittune-pty-tool-"));
	await configureTestAgent(root, port);
	const env = {
		...process.env,
		BITTUNE_HOME: root,
		BITTUNE_PTY_API_KEY: "test-key",
		TERM: "xterm-256color",
	};
	let first: PtyRun | undefined;
	let resumed: PtyRun | undefined;
	try {
		first = spawnBittunePty(["list deployment presets"], env);
		const firstOutput = await first.waitForOutput((output) => output.includes("PTY Tool Call continuation passed."));
		assert.match(firstOutput, /list_deployment_presets/);
		first.write("/quit\r");
		const firstExit = await first.waitForExit();
		assert.equal(firstExit.code, 0);
		const resumeMatch = firstExit.output.match(/To resume this session:\s*bittune --session ([A-Za-z0-9_-]+)/);
		assert.ok(resumeMatch, firstExit.output);
		const sessionId = resumeMatch[1]!;

		resumed = spawnBittunePty(["--session", sessionId], env);
		const resumedOutput = await resumed.waitForOutput((output) => output.includes("PTY Tool Call continuation passed."));
		assert.match(resumedOutput, /PTY Tool Call continuation passed\./);
		resumed.write("/quit\r");
		const resumedExit = await resumed.waitForExit();
		assert.equal(resumedExit.code, 0);
		assert.equal(requests, 3, "resuming a session must not send a new Agent prompt");
	} finally {
		first?.terminate();
		resumed?.terminate();
		await rm(root, { recursive: true, force: true });
		await closeServer(server);
	}
});

test("Bittune TUI cancels a streaming turn with the Pi Escape binding", { skip: !hasLinuxScript }, async () => {
	let requestClosed = false;
	const server = createServer((request, response) => {
		request.on("close", () => {
			requestClosed = true;
		});
		request.resume();
		request.on("end", () => {
			response.writeHead(200, {
				"content-type": "text/event-stream",
				connection: "keep-alive",
				"cache-control": "no-cache",
			});
			response.write(
				'data: {"id":"pty-abort","object":"chat.completion.chunk","created":1,"model":"pty-agent-model","choices":[{"index":0,"delta":{"role":"assistant","content":"partial response"},"finish_reason":null}]}\n\n',
			);
		});
	});
	const port = await listen(server);
	const root = await mkdtemp(join(tmpdir(), "bittune-pty-abort-"));
	await configureTestAgent(root, port);
	const env = {
		...process.env,
		BITTUNE_HOME: root,
		BITTUNE_PTY_API_KEY: "test-key",
		TERM: "xterm-256color",
	};
	let run: PtyRun | undefined;
	try {
		run = spawnBittunePty(["cancel this turn"], env);
		await run.waitForOutput((output) => output.includes("partial response"));
		run.write("\u001b");
		const abortedOutput = await run.waitForOutput((output) => output.includes("Operation aborted"));
		assert.match(abortedOutput, /Operation aborted/);
		const startedAt = Date.now();
		while (!requestClosed && Date.now() - startedAt < 2_000) {
			await new Promise((resolve) => setTimeout(resolve, 25));
		}
		assert.equal(requestClosed, true, "cancelling the turn must abort the provider request");
		run.write("/quit\r");
		const exit = await run.waitForExit();
		assert.equal(exit.code, 0);
	} finally {
		run?.terminate();
		await rm(root, { recursive: true, force: true });
		await closeServer(server);
	}
});
