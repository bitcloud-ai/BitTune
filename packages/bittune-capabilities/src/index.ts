import type { ExtensionAPI, ExtensionFactory, ToolDefinition, ToolResultEvent } from "../../bittune-runtime/src/core/extensions/types.ts";
import {
  CAPABILITY_SESSION_ENTRY,
  activeToolNamesForCapabilities,
  capabilitiesFromSessionEntries,
  createCapabilityActivationTool,
  type BittuneCapability,
} from "./shared/capability-catalog.ts";
import { RunRecorder } from "./shared/run-recorder.ts";
import { StateStore } from "./shared/state-store.ts";
import { createToolRegistry } from "./tool-registry.ts";

export interface BittuneExtensionOptions {
  stateRoot?: string;
  namespaceId?: string;
  initialCapabilities?: readonly BittuneCapability[];
  /** Read-only MCP tools prepared by the controlled runtime before session creation. */
  externalTools?: readonly ToolDefinition[];
  /** Closes connections backing externally registered tools on session shutdown. */
  closeExternalTools?: () => Promise<void>;
}

function bashMayHaveChangedManagedState(event: ToolResultEvent): boolean {
  const command = typeof event.input.command === "string" ? event.input.command.trim() : "";
  if (!command || /[;&|`\n]/.test(command)) return true;
  // This is intentionally an allowlist. Unknown commands invalidate cached
  // observations, while common diagnostics do not cause needless re-probes.
  return !/^(?:nvidia-smi|docker\s+(?:ps|container\s+(?:ls|inspect|logs)|image\s+inspect|version|info)|(?:cat|ls|grep|find|df|free|uname|lscpu|pwd|which)\b)/.test(command);
}

export function createBittuneExtension(options: BittuneExtensionOptions = {}): ExtensionFactory {
  return (pi: ExtensionAPI): void => {
    const activeCapabilities = new Set<BittuneCapability>(options.initialCapabilities ?? []);
    const externalToolNames = (options.externalTools ?? []).map((tool) => tool.name);
    const applyCapabilities = (capabilities: Iterable<BittuneCapability>): void => {
      activeCapabilities.clear();
      for (const capability of capabilities) activeCapabilities.add(capability);
      pi.setActiveTools([...activeToolNamesForCapabilities(activeCapabilities), ...externalToolNames]);
    };
    const activate = (capability: BittuneCapability) => {
      const already_active = activeCapabilities.has(capability);
      if (!already_active) {
        pi.appendEntry(CAPABILITY_SESSION_ENTRY, { capability, activated_at: new Date().toISOString() });
        activeCapabilities.add(capability);
      }
      const active_tool_names = [...activeToolNamesForCapabilities(activeCapabilities), ...externalToolNames];
      pi.setActiveTools(active_tool_names);
      return { already_active, active_tool_names };
    };

    for (const tool of [...createToolRegistry(options.stateRoot, options.namespaceId), ...(options.externalTools ?? [])]) pi.registerTool(tool);
    pi.registerTool(createCapabilityActivationTool(new RunRecorder(new StateStore(options.stateRoot, options.namespaceId)), activate));
    pi.on("session_start", async (_event, context) => {
      applyCapabilities(capabilitiesFromSessionEntries(context.sessionManager.getBranch()));
    });
    pi.on("session_shutdown", async () => {
      await options.closeExternalTools?.();
    });
    // Bash is Session-only. Unknown or mutating host commands invalidate
    // managed-service observations until a domain inspect/probe refreshes them.
    pi.on("tool_result", async (event: ToolResultEvent) => {
      if (event.toolName !== "bash" || !bashMayHaveChangedManagedState(event)) return;
      const store = new StateStore(options.stateRoot, options.namespaceId);
      try {
        await store.markServicesStale("bash completed; re-inspect or probe before relying on service state");
      } finally {
        store.close();
      }
    });
  };
}

export default function bittune(pi: ExtensionAPI): void {
  createBittuneExtension()(pi);
}
