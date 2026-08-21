import { BittuneError } from "./errors.ts";
import { runCommand } from "./process.ts";

export interface VllmCliProfile {
  image_reference: string;
  version?: string;
  flags: string[];
  supports_swap_space: boolean;
  diagnostics: string;
}

function extractFlags(help: string): string[] {
  return Array.from(new Set(help.match(/--[a-z][a-z0-9-]*/gi) ?? [])).sort();
}

function extractVersion(value: string): string | undefined {
  return /vllm(?:\s*,?\s*version)?\s*[:=]?\s*(v?\d+(?:\.\d+)+(?:[-+][\w.-]+)?)/i.exec(value)?.[1];
}

const REQUIRED_FLAGS = ["--model", "--served-model-name", "--host", "--port"];

function hasRequiredFlags(flags: string[]): boolean {
  return REQUIRED_FLAGS.every((flag) => flags.includes(flag));
}

/**
 * The managed adapter must interrogate the selected image instead of assuming
 * that a project-wide vLLM flag set matches every image digest.
 */
export async function inspectVllmCliProfile(input: { image_reference: string; run_reference: string }, signal?: AbortSignal): Promise<VllmCliProfile> {
  const [helpAll, version] = await Promise.all([
    runCommand("docker", ["run", "--rm", "--pull=never", "--entrypoint", "vllm", input.run_reference, "serve", "--help=all"], { signal, timeoutMs: 60_000, maxBytes: 2 * 1024 * 1024 }),
    runCommand("docker", ["run", "--rm", "--pull=never", "--entrypoint", "vllm", input.run_reference, "--version"], { signal, timeoutMs: 60_000, maxBytes: 64 * 1024 }),
  ]);
  const attempts = [{ command: "vllm serve --help=all", result: helpAll }];
  let help = helpAll;
  let flags = extractFlags(`${helpAll.stdout}\n${helpAll.stderr}`);
  if (helpAll.exit_code !== 0 || !hasRequiredFlags(flags)) {
    const fallback = await runCommand("docker", ["run", "--rm", "--pull=never", "--entrypoint", "vllm", input.run_reference, "serve", "--help"], { signal, timeoutMs: 60_000, maxBytes: 2 * 1024 * 1024 });
    attempts.push({ command: "vllm serve --help", result: fallback });
    help = fallback;
    flags = extractFlags(`${fallback.stdout}\n${fallback.stderr}`);
  }
  const diagnostics = `${attempts.map((attempt) => `$ ${attempt.command}\n${attempt.result.stdout}\n${attempt.result.stderr}`).join("\n")}\n$ vllm --version\n${version.stdout}\n${version.stderr}`;
  if (help.exit_code !== 0) {
    throw new BittuneError("runtime_cli_unavailable", `Unable to read vLLM CLI help from ${input.image_reference}.`, false, {
      image_reference: input.image_reference,
      exit_code: help.exit_code,
      diagnostics: diagnostics.slice(-8_000),
    });
  }
  for (const flag of REQUIRED_FLAGS) {
    if (!flags.includes(flag)) {
      throw new BittuneError("runtime_cli_incompatible", `Runtime image ${input.image_reference} does not expose ${flag} through vllm serve.`, false, {
        image_reference: input.image_reference,
        missing_flag: flag,
        diagnostics: diagnostics.slice(-8_000),
      });
    }
  }
  const detectedVersion = extractVersion(`${version.stdout}\n${version.stderr}\n${help.stdout}`);
  return {
    image_reference: input.image_reference,
    ...(detectedVersion ? { version: detectedVersion } : {}),
    flags,
    supports_swap_space: flags.includes("--swap-space"),
    diagnostics,
  };
}
