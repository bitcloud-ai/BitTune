import type { ToolResultEvent } from "../../../bittune-runtime/src/core/extensions/types.ts";

/**
 * Deprecated compatibility hook. Bash is deliberately Session-only: an
 * arbitrary command cannot establish a durable domain fact. The extension
 * marks managed service observations stale instead.
 */
export async function recordBashAudit(_root: string | undefined, _event: ToolResultEvent): Promise<void> {
  // Intentionally no-op. Keep the export temporarily for extension compatibility.
}
