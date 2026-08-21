import type { ToolDefinition } from "../../bittune-runtime/src/core/extensions/types.ts";
import { createAssetTools } from "./capabilities/assets/tools.ts";
import { createBenchmarkTools } from "./capabilities/benchmark/tools.ts";
import { createCapacityBaselineTools } from "./capabilities/capacity-baselines/tools.ts";
import { createDeploymentPresetTools } from "./capabilities/deployment-presets/tools.ts";
import { createDeploymentOptionTools } from "./capabilities/deployment-options/tools.ts";
import { createDiscoveryTools } from "./capabilities/discovery/tools.ts";
import { createEnvironmentTools } from "./capabilities/environment/tools.ts";
import { createEvidenceTools } from "./capabilities/evidence/tools.ts";
import { createExperimentTools } from "./capabilities/experiments/tools.ts";
import { createServingTools } from "./capabilities/serving/tools.ts";
import { RunRecorder } from "./shared/run-recorder.ts";
import { StateStore } from "./shared/state-store.ts";

export function createToolRegistry(root?: string, namespaceId?: string): ToolDefinition[] {
  const store = new StateStore(root, namespaceId);
  const recorder = new RunRecorder(store);
  return [
    ...createEnvironmentTools(recorder),
    ...createDiscoveryTools(recorder),
    ...createDeploymentOptionTools(recorder),
    ...createExperimentTools(recorder),
    ...createDeploymentPresetTools(recorder),
    ...createCapacityBaselineTools(recorder),
    ...createAssetTools(recorder),
    ...createServingTools(recorder),
    ...createBenchmarkTools(recorder),
    ...createEvidenceTools(recorder),
  ];
}
