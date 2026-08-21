import type { StableToolError } from "./observation.ts";

export class BittuneError extends Error {
  readonly stable: StableToolError;

  constructor(code: string, message: string, retryable = false, details?: Record<string, unknown>) {
    super(message);
    this.name = "BittuneError";
    this.stable = { code, message, retryable, ...(details === undefined ? {} : { details }) };
  }
}

export function asBittuneError(error: unknown): BittuneError {
  if (error instanceof BittuneError) return error;
  if (error instanceof Error && error.name === "AbortError") {
    return new BittuneError("cancelled", "操作已取消。", true);
  }
  return new BittuneError("provider_error", "底层 Provider 执行失败。", true, {
    cause: error instanceof Error ? error.message : String(error),
  });
}
