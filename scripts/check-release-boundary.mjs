import { execFileSync } from "node:child_process";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

const npmCommand = process.platform === "win32" ? "npm.cmd" : "npm";
const packageJson = JSON.parse(await readFile("package.json", "utf8"));
const npmCli = process.env.npm_execpath;
const npmInvocation = npmCli ? [process.execPath, [npmCli, "pack", "--dry-run", "--json"]] : [npmCommand, ["pack", "--dry-run", "--json"]];
const packed = JSON.parse(
	execFileSync(npmInvocation[0], npmInvocation[1], {
		encoding: "utf8",
		shell: process.platform === "win32" && !npmCli,
	}),
)[0];
const paths = packed.files.map(({ path }) => path).sort();
const expectedPaths = [
	"LICENSE",
	"README.md",
	"THIRD_PARTY_NOTICES.md",
	"dist/bittune.js",
	"dist/modes/interactive/theme/dark.json",
	"dist/modes/interactive/theme/light.json",
	"guide/README.md",
	"guide/getting-started.md",
	"guide/operations.md",
	"install/build-offline-bundle-ubuntu.sh",
	"install/install-ubuntu.sh",
	"install/offline-install-ubuntu.sh",
	"install/offline-manifest.env",
	"install/requirements.txt",
	"package.json",
].sort();

if (JSON.stringify(paths) !== JSON.stringify(expectedPaths)) {
	throw new Error(
		"Unexpected release package contents.\nExpected: " +
			expectedPaths.join(", ") +
			"\nActual: " +
			paths.join(", "),
	);
}
if (packageJson.bin?.bittune !== "dist/bittune.js" || Object.keys(packageJson.bin ?? {}).length !== 1) {
	throw new Error("Release package must expose exactly the Bittune CLI entrypoint.");
}
if (packageJson.piConfig || packageJson.name !== "@bittune/runtime") {
	throw new Error("Release package contains an upstream product configuration surface.");
}

const help = execFileSync(process.execPath, [resolve("dist/bittune.js"), "--help"], { encoding: "utf8" });
if (!/\bBittune\b/.test(help) || /\bpi\b/i.test(help)) {
	throw new Error("Bittune help contains an unexpected product surface:\n" + help);
}

try {
	execFileSync(process.execPath, [resolve("dist/bittune.js"), "--session-dir", "/tmp/unsupported"], {
		encoding: "utf8",
		stdio: ["ignore", "pipe", "pipe"],
	});
	throw new Error("Unsupported --session-dir unexpectedly succeeded.");
} catch (error) {
	if (!(error && typeof error === "object" && "status" in error && error.status !== 0)) {
		throw error;
	}
	const output = String(error.stdout ?? "") + "\n" + String(error.stderr ?? "");
	if (!/Unknown option|Bittune/i.test(output) || /pi-coding-agent|pi\.dev/i.test(output)) {
		throw new Error("Unsupported option did not produce a Bittune-only error:\n" + output);
	}
}

console.log("Release boundary passed for " + packed.id + ": " + paths.length + " files.");
