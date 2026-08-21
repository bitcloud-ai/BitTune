import { execFileSync } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { dirname } from "node:path";

const outputPath = process.argv[2];
if (!outputPath) throw new Error("Usage: node scripts/generate-sbom.mjs <output-path>");
const npmCommand = process.platform === "win32" ? "npm.cmd" : "npm";
const npmCli = process.env.npm_execpath;
const npmInvocation = npmCli
	? [process.execPath, [npmCli, "sbom", "--package-lock-only", "--omit=dev", "--sbom-format", "cyclonedx", "--sbom-type", "application"]]
	: [npmCommand, ["sbom", "--package-lock-only", "--omit=dev", "--sbom-format", "cyclonedx", "--sbom-type", "application"]];
const sbom = execFileSync(
	npmInvocation[0],
	npmInvocation[1],
	{ encoding: "utf8", shell: process.platform === "win32" && !npmCli },
);
JSON.parse(sbom);
await mkdir(dirname(outputPath), { recursive: true });
await writeFile(outputPath, sbom, "utf8");
console.log("SBOM written to " + outputPath);
