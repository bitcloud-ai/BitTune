export interface BittuneConfigureOptions {
	baseUrl?: string;
	modelId?: string;
	providerId?: string;
	apiKeyEnv?: string;
}

export type BittuneMcpCommand =
	| { kind: "help" }
	| { kind: "list" }
	| { kind: "get"; name: string }
	| { kind: "test"; name?: string };

export type BittuneCliInvocation =
	| { kind: "help" }
	| { kind: "version" }
	| { kind: "doctor" }
	| { kind: "configure"; options: BittuneConfigureOptions }
	| { kind: "mcp"; command: BittuneMcpCommand }
	| { kind: "interactive"; sessionId?: string; message?: string };

const CONFIGURE_OPTIONS: Readonly<Record<string, keyof BittuneConfigureOptions>> = {
	"--base-url": "baseUrl",
	"--model-id": "modelId",
	"--provider-id": "providerId",
	"--api-key-env": "apiKeyEnv",
};

function usageError(message: string): Error {
	return new Error(`${message} Run \`bittune --help\` for usage.`);
}

function parseOptions(args: string[], allowed: ReadonlySet<string>, command: string): Map<string, string> {
	const values = new Map<string, string>();
	for (let index = 0; index < args.length; index += 1) {
		const arg = args[index]!;
		if (!arg.startsWith("-")) throw usageError(`Unexpected argument \"${arg}\" for ${command}.`);
		if (!allowed.has(arg)) throw usageError(`Unknown option \"${arg}\" for ${command}.`);
		if (values.has(arg)) throw usageError(`Option \"${arg}\" may only be specified once.`);
		const value = args[index + 1];
		if (value === undefined || value.startsWith("-")) {
			throw usageError(`Option \"${arg}\" requires a value.`);
		}
		values.set(arg, value);
		index += 1;
	}
	return values;
}

function parseConfigure(args: string[]): BittuneCliInvocation {
	if (args.length === 1 && (args[0] === "--help" || args[0] === "-h")) return { kind: "help" };
	const values = parseOptions(args, new Set(Object.keys(CONFIGURE_OPTIONS)), "bittune configure");
	const options: BittuneConfigureOptions = {};
	for (const [flag, key] of Object.entries(CONFIGURE_OPTIONS)) {
		const value = values.get(flag);
		if (value !== undefined) options[key] = value;
	}
	return { kind: "configure", options };
}

function parseMcp(args: string[]): BittuneCliInvocation {
	const [command, ...rest] = args;
	if (command === undefined || command === "--help" || command === "-h" || command === "help") {
		if (rest.length > 0) throw usageError("The mcp help command does not accept arguments.");
		return { kind: "mcp", command: { kind: "help" } };
	}
	if (command === "list") {
		if (rest.length > 0) throw usageError("The mcp list command does not accept arguments.");
		return { kind: "mcp", command: { kind: "list" } };
	}
	if (command === "get") {
		if (rest.length !== 1 || rest[0]!.startsWith("-")) throw usageError("Usage: bittune mcp get <name>.");
		return { kind: "mcp", command: { kind: "get", name: rest[0]! } };
	}
	if (command === "test") {
		if (rest.length > 1 || rest[0]?.startsWith("-")) throw usageError("Usage: bittune mcp test [name].");
		return { kind: "mcp", command: { kind: "test", ...(rest[0] ? { name: rest[0] } : {}) } };
	}
	throw usageError(`Unknown mcp command "${command}".`);
}

function parseInteractive(args: string[]): BittuneCliInvocation {
	const values = new Map<string, string>();
	const messages: string[] = [];
	for (let index = 0; index < args.length; index += 1) {
		const arg = args[index]!;
		if (arg === "--session") {
			if (values.has(arg)) throw usageError("Option \"--session\" may only be specified once.");
			const value = args[index + 1];
			if (value === undefined || value.startsWith("-")) throw usageError("Option \"--session\" requires a value.");
			values.set(arg, value);
			index += 1;
			continue;
		}
		if (arg.startsWith("-")) throw usageError(`Unknown option \"${arg}\" for bittune.`);
		messages.push(arg);
	}
	const sessionId = values.get("--session");
	if (sessionId && messages.length > 0) {
		throw usageError("A session resume cannot be combined with an initial message.");
	}
	return {
		kind: "interactive",
		...(sessionId ? { sessionId } : {}),
		...(messages.length > 0 ? { message: messages.join(" ") } : {}),
	};
}

export function parseBittuneCliArguments(argv: string[]): BittuneCliInvocation {
	const [command, ...args] = argv;
	if (command === undefined) return { kind: "interactive" };
	if (command === "--help" || command === "-h" || command === "help") {
		if (args.length > 0) throw usageError(`Unexpected argument \"${args[0]}\".`);
		return { kind: "help" };
	}
	if (command === "--version" || command === "-v" || command === "version") {
		if (args.length > 0) throw usageError(`Unexpected argument \"${args[0]}\" for version.`);
		return { kind: "version" };
	}
	if (command === "configure") return parseConfigure(args);
	if (command === "mcp") return parseMcp(args);
	if (command === "doctor") {
		if (args.length > 0) throw usageError("The doctor command does not accept arguments.");
		return { kind: "doctor" };
	}
	return parseInteractive(argv);
}
