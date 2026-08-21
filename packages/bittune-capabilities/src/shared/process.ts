import { spawn } from "node:child_process";
import { BittuneError } from "./errors.ts";

const TERMINATION_GRACE_MS = 10_000;

export interface CommandResult {
  command: string;
  args: string[];
  exit_code: number | null;
  stdout: string;
  stderr: string;
  duration_ms: number;
  truncated: boolean;
}

export async function runCommand(command: string, args: string[], options: {
  signal?: AbortSignal | undefined;
  timeoutMs?: number;
  maxBytes?: number;
  cwd?: string;
  env?: NodeJS.ProcessEnv;
} = {}): Promise<CommandResult> {
  const started = performance.now();
  const maxBytes = options.maxBytes ?? 1024 * 1024;
  return new Promise<CommandResult>((resolve, reject) => {
    let stdout = "";
    let stderr = "";
    let bytes = 0;
    let truncated = false;
    let timedOut = false;
    let terminationRequested = false;
    let forceKill: NodeJS.Timeout | undefined;
    const child = spawn(command, args, {
      cwd: options.cwd,
      env: options.env,
      shell: false,
      windowsHide: true,
      detached: process.platform !== "win32",
      stdio: ["ignore", "pipe", "pipe"],
    });
    const append = (target: "stdout" | "stderr", chunk: Buffer) => {
      const remaining = maxBytes - bytes;
      if (remaining <= 0) { truncated = true; return; }
      const part = chunk.subarray(0, remaining).toString("utf8");
      bytes += Buffer.byteLength(part);
      if (chunk.length > remaining) truncated = true;
      if (target === "stdout") stdout += part; else stderr += part;
    };
    child.stdout.on("data", (chunk: Buffer) => append("stdout", chunk));
    child.stderr.on("data", (chunk: Buffer) => append("stderr", chunk));
    const signalProcessGroup = (signal: NodeJS.Signals) => {
      if (!child.pid) return;
      if (process.platform === "win32") {
        if (signal === "SIGTERM") child.kill("SIGTERM");
        else spawn("taskkill", ["/pid", String(child.pid), "/t", "/f"], { shell: false, windowsHide: true, stdio: "ignore" }).once("error", () => undefined);
        return;
      }
      try { process.kill(-child.pid, signal); }
      catch (error: unknown) { if ((error as NodeJS.ErrnoException).code !== "ESRCH") child.kill(signal); }
    };
    const stop = () => {
      if (terminationRequested) return;
      terminationRequested = true;
      signalProcessGroup("SIGTERM");
      forceKill = setTimeout(() => signalProcessGroup("SIGKILL"), TERMINATION_GRACE_MS);
      forceKill.unref();
    };
    options.signal?.addEventListener("abort", stop, { once: true });
    const timeout = options.timeoutMs === undefined ? undefined : setTimeout(() => { timedOut = true; stop(); }, options.timeoutMs);
    child.once("error", (error: NodeJS.ErrnoException) => {
      if (timeout) clearTimeout(timeout);
      if (forceKill) clearTimeout(forceKill);
      options.signal?.removeEventListener("abort", stop);
      if (error.code === "ENOENT") reject(new BittuneError("provider_unavailable", `${command} 未安装或不在 PATH 中。`, false));
      else reject(error);
    });
    child.once("close", (exitCode) => {
      if (timeout) clearTimeout(timeout);
      if (forceKill && !terminationRequested) clearTimeout(forceKill);
      options.signal?.removeEventListener("abort", stop);
      if (options.signal?.aborted) return reject(new BittuneError("cancelled", "操作已取消。", true));
      if (timedOut) return reject(new BittuneError("timeout", `${command} 在限定时间内未完成。`, true));
      resolve({ command, args, exit_code: exitCode, stdout, stderr, duration_ms: Math.round(performance.now() - started), truncated });
    });
  });
}

export function requireSuccess(result: CommandResult, code = "provider_error"): CommandResult {
  if (result.exit_code !== 0) {
    throw new BittuneError(code, `${result.command} 执行失败（退出码 ${result.exit_code ?? "unknown"}）。`, true, {
      exit_code: result.exit_code,
      stderr: result.stderr.slice(0, 2000),
    });
  }
  return result;
}
