import { createHash } from "node:crypto";
import { homedir } from "node:os";
import { delimiter, relative, resolve } from "node:path";

export type DiscoverySource = "docker" | "process" | "gpu_stack" | "huggingface" | "modelscope" | "endpoint";
export type ManagementStatus = "externally_managed" | "bittune_managed" | "unknown";
export type DiscoveryConfidence = "exact" | "inferred" | "unknown";

export interface DiscoveryFields {
  source: DiscoverySource;
  management_status: ManagementStatus;
  confidence: DiscoveryConfidence;
  observed_at: string;
  source_run_id: string;
  discovery_ref: string;
}

export interface DiscoveredRuntime extends DiscoveryFields {
  runtime_id: string;
  runtime_type: "vllm" | "gpustack" | "tgi" | "triton" | "unknown";
  name: string;
  version?: string;
  container_id?: string;
  process_id?: number;
  runtime_image_repository?: string;
  runtime_image_digest?: string;
  runtime_image_id?: string;
}

export interface LocalModelArtifact extends DiscoveryFields {
  artifact_ref: string;
  artifact_type: "model_snapshot";
  model_id: string;
  revision?: string;
  cache_kind: "huggingface" | "modelscope" | "unknown";
  weight_file_count: number;
  size_bytes: number;
  metadata_fingerprint: string;
  metadata: {
    architecture?: string;
    declared_dtype?: string;
    declared_context_tokens?: number;
    quantization?: string;
    tokenizer_class?: string;
    chat_template_available?: boolean;
    generation_defaults_available?: boolean;
    indexed_weight_count?: number;
  };
}

export interface DiscoveredInferenceService extends DiscoveryFields {
  service_ref: string;
  service_type: "vllm" | "gpustack" | "tgi" | "triton" | "openai_compatible" | "unknown";
  name: string;
  endpoint_url?: string;
  model_ids?: string[];
  container_id?: string;
  process_id?: number;
  instance_id?: string;
}

export function discoveryRef(kind: string, identity: string): string {
  return "discovery-" + createHash("sha256").update(kind + ":" + identity).digest("hex").slice(0, 32);
}

export function fields(source: DiscoverySource, managementStatus: ManagementStatus, confidence: DiscoveryConfidence, runId: string, identity: string, observedAt = new Date().toISOString()): DiscoveryFields {
  return { source, management_status: managementStatus, confidence, observed_at: observedAt, source_run_id: runId, discovery_ref: discoveryRef(source, identity) };
}

export function configuredModelRoots(): Array<{ kind: "huggingface" | "modelscope" | "unknown"; root: string }> {
  const configured = process.env.BITTUNE_MODEL_CACHE_ROOTS?.split(delimiter).map((item) => item.trim()).filter(Boolean);
  if (configured?.length) return configured.map((root) => ({ kind: "unknown", root: resolve(root) }));
  const hfHome = process.env.HF_HOME || resolve(homedir(), ".cache", "huggingface");
  const modelScope = process.env.MODELSCOPE_CACHE || resolve(homedir(), ".cache", "modelscope");
  return [{ kind: "huggingface", root: resolve(hfHome) }, { kind: "modelscope", root: resolve(modelScope) }];
}

export function relativeToRoot(root: string, path: string): string {
  return relative(root, path).replaceAll("\\", "/");
}

export function runtimeTypeFromImage(image: string): DiscoveredRuntime["runtime_type"] {
  const normalized = image.toLowerCase();
  if (normalized.includes("vllm")) return "vllm";
  if (normalized.includes("gpustack")) return "gpustack";
  if (normalized.includes("text-generation-inference") || normalized.includes("/tgi")) return "tgi";
  if (normalized.includes("triton")) return "triton";
  return "unknown";
}

export function serviceTypeFromRuntime(runtimeType: DiscoveredRuntime["runtime_type"]): DiscoveredInferenceService["service_type"] {
  return runtimeType === "unknown" ? "unknown" : runtimeType;
}
