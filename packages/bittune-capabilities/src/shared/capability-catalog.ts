import { Type } from "typebox";
import type { ToolDefinition } from "../../../bittune-runtime/src/core/extensions/types.ts";
import { RunRecorder } from "./run-recorder.ts";
import { createBittuneTool } from "./tool.ts";

export const CAPABILITY_ACTIVATION_TOOL = "activate_capability";
export const CAPABILITY_SESSION_ENTRY = "bittune.capability-activated";

export const BITTUNE_CAPABILITIES = ["deployment", "serving", "benchmark", "capacity", "experiments"] as const;
export type BittuneCapability = (typeof BITTUNE_CAPABILITIES)[number];

export const CORE_TOOL_NAMES = [
  "read",
  "bash",
  "inspect_gpu",
  "inspect_linux_host",
  "inspect_container_runtime",
  "list_inference_runtimes",
  "inspect_inference_runtime",
  "list_local_model_artifacts",
  "list_inference_services",
  "inspect_inference_service",
  "probe_inference_endpoint",
  "list_run_records",
  "get_run_record",
  "read_artifact_excerpt",
  CAPABILITY_ACTIVATION_TOOL,
] as const;

export const CAPABILITY_TOOL_NAMES: Readonly<Record<BittuneCapability, readonly string[]>> = {
  deployment: [
    "derive_deployment_options",
    "list_deployment_presets",
    "get_deployment_preset",
    "publish_deployment_preset",
    "inspect_vllm_image",
    "pull_vllm_image",
    "inspect_model_snapshot",
    "download_model_snapshot",
    "inspect_vllm_capabilities",
  ],
  serving: [
    "list_vllm_services",
    "start_vllm_service",
    "wait_for_vllm_ready",
    "read_vllm_service_logs",
    "inspect_vllm_service",
    "probe_vllm_endpoint",
    "stop_vllm_service",
  ],
  benchmark: ["run_performance_test", "analyze_benchmark_artifact"],
  capacity: [
    "list_capacity_baselines",
    "get_capacity_baseline",
    "derive_capacity_baseline",
    "publish_capacity_baseline",
  ],
  experiments: [
    "publish_experiment_spec",
    "list_experiment_specs",
    "get_experiment_spec",
    "record_experiment_trial",
    "list_experiment_trials",
    "get_experiment_trial",
    "derive_experiment_comparison",
    "publish_experiment_comparison",
    "list_experiment_comparisons",
    "get_experiment_comparison",
  ],
};

type SessionEntryLike = {
  type?: unknown;
  customType?: unknown;
  data?: unknown;
};

export function isBittuneCapability(value: unknown): value is BittuneCapability {
  return typeof value === "string" && (BITTUNE_CAPABILITIES as readonly string[]).includes(value);
}

export function capabilitiesFromSessionEntries(entries: readonly SessionEntryLike[]): BittuneCapability[] {
  const activated = new Set<BittuneCapability>();
  for (const entry of entries) {
    if (entry.type !== "custom" || entry.customType !== CAPABILITY_SESSION_ENTRY || !entry.data || typeof entry.data !== "object") continue;
    const capability = (entry.data as { capability?: unknown }).capability;
    if (isBittuneCapability(capability)) activated.add(capability);
  }
  return BITTUNE_CAPABILITIES.filter((capability) => activated.has(capability));
}

export function activeToolNamesForCapabilities(capabilities: Iterable<BittuneCapability>): string[] {
  const names = new Set<string>(CORE_TOOL_NAMES);
  for (const capability of capabilities) {
    for (const name of CAPABILITY_TOOL_NAMES[capability]) names.add(name);
  }
  return [...names];
}

export function createCapabilityActivationTool(
  recorder: RunRecorder,
  activate: (capability: BittuneCapability) => { already_active: boolean; active_tool_names: string[] },
): ToolDefinition {
  return createBittuneTool({
    name: CAPABILITY_ACTIVATION_TOOL,
    label: "启用 Bittune Capability",
    recording: "session",
    recorder,
    parameters: Type.Object({
      capability: Type.Union([
        Type.Literal("deployment"),
        Type.Literal("serving"),
        Type.Literal("benchmark"),
        Type.Literal("capacity"),
        Type.Literal("experiments"),
      ]),
    }),
    description: "按用户明确目标启用一组已编译、受审查的 Bittune Tool。它只改变下一回合可见的 Tool，不执行下载、部署、停止、压测或实验，也不加载远程 Tool、Extension 或 Skill。",
    async execute(params) {
      const capability = (params as { capability: BittuneCapability }).capability;
      const result = activate(capability);
      return {
        summary: result.already_active
          ? `Capability ${capability} 已启用。`
          : `已启用 Capability ${capability}；下一回合可使用对应 Tool。`,
        provenance_type: "stored",
        data: { capability, ...result },
        provider: { name: "bittune-capability-catalog" },
      };
    },
  });
}
