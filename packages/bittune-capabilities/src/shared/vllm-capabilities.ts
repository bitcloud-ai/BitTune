import { BittuneError } from "./errors.ts";
import { canonicalJson, sha256 } from "./identifiers.ts";

export const VLLM_CAPABILITY_VERSION = "vllm-managed-v2";

export type ServingScalar = boolean | number | string | number[];
export type ServingConfiguration = Record<string, ServingScalar>;

export interface ServingParameterDescriptor {
  name: string;
  type: "boolean" | "integer" | "number" | "enum" | "integer_array";
  unit?: string;
  default: ServingScalar;
  minimum?: number;
  exclusive_minimum?: boolean;
  maximum?: number;
  values?: string[];
  dependencies?: string[];
  conflicts?: string[];
  description: string;
}

export const VLLM_SERVING_PARAMETERS: ServingParameterDescriptor[] = [
  { name: "dtype", type: "enum", default: "auto", values: ["auto", "bfloat16", "float16", "float32"], description: "Model execution dtype." },
  { name: "tensor_parallel_size", type: "integer", default: 1, minimum: 1, description: "Tensor parallel worker count. Actual availability depends on selected GPUs and Runtime observation." },
  { name: "pipeline_parallel_size", type: "integer", default: 1, minimum: 1, description: "Pipeline parallel worker count. Actual availability depends on selected GPUs and Runtime observation." },
  { name: "gpu_device_ids", type: "integer_array", default: [], minimum: 0, dependencies: ["tensor_parallel_size", "pipeline_parallel_size"], description: "Explicit GPU indexes for this managed container. Empty means Docker selects all visible GPUs." },
  { name: "max_model_len", type: "integer", unit: "tokens", default: 32768, minimum: 1, description: "Maximum model context length. The selected model and Runtime decide whether it is valid." },
  { name: "max_num_seqs", type: "integer", default: 16, minimum: 1, description: "Maximum concurrent sequences. Actual capacity requires measurement." },
  { name: "max_num_batched_tokens", type: "integer", unit: "tokens", default: 8192, minimum: 1, description: "Scheduler token budget per batch." },
  { name: "gpu_memory_utilization", type: "number", default: 0.9, minimum: 0, exclusive_minimum: true, maximum: 1, description: "Fraction of each selected GPU reserved by vLLM." },
  { name: "swap_space_gib", type: "number", unit: "GiB", default: 4, minimum: 0, description: "Host swap space per GPU available to vLLM." },
  { name: "enforce_eager", type: "boolean", default: false, description: "Disable CUDA graph execution." },
  { name: "enable_prefix_caching", type: "boolean", default: true, description: "Enable automatic prefix caching." },
];

const descriptorByName = new Map(VLLM_SERVING_PARAMETERS.map((item) => [item.name, item]));

export function vllmCapabilitySnapshot(runtime?: { image_repository: string; image_digest: string }) {
  return {
    runtime_kind: "vllm" as const,
    capability_version: VLLM_CAPABILITY_VERSION,
    ...(runtime ? { runtime } : {}),
    parameters: VLLM_SERVING_PARAMETERS,
    unsupported: [
      { category: "arbitrary_engine_args", reason: "The managed adapter only accepts the declared serving_configuration fields." },
      { category: "runtime_kinds", reason: "Only the managed vLLM adapter is implemented in this release." },
    ],
  };
}

function invalid(name: string, message: string): never {
  throw new BittuneError("invalid_serving_configuration", `${name}: ${message}`, false);
}

function assertDescriptorValue(descriptor: ServingParameterDescriptor, value: unknown): ServingScalar {
  if (descriptor.type === "boolean") {
    if (typeof value !== "boolean") invalid(descriptor.name, "must be boolean");
    return value;
  }
  if (descriptor.type === "enum") {
    if (typeof value !== "string" || !descriptor.values?.includes(value)) invalid(descriptor.name, `must be one of ${descriptor.values?.join(", ")}`);
    return value;
  }
  if (descriptor.type === "integer_array") {
    if (!Array.isArray(value) || value.some((item) => !Number.isInteger(item) || item < (descriptor.minimum ?? -Infinity) || item > (descriptor.maximum ?? Infinity))) invalid(descriptor.name, "must be an array of supported GPU indexes");
    if (new Set(value).size !== value.length) invalid(descriptor.name, "must not contain duplicate GPU indexes");
    return [...value] as number[];
  }
  if (typeof value !== "number" || !Number.isFinite(value) || (descriptor.type === "integer" && !Number.isInteger(value))) invalid(descriptor.name, `must be a finite ${descriptor.type}`);
  if (descriptor.minimum !== undefined && (descriptor.exclusive_minimum ? value <= descriptor.minimum : value < descriptor.minimum)) invalid(descriptor.name, `must be ${descriptor.exclusive_minimum ? ">" : ">="} ${descriptor.minimum}`);
  if (descriptor.maximum !== undefined && value > descriptor.maximum) invalid(descriptor.name, `must be <= ${descriptor.maximum}`);
  return value;
}

export function materializeVllmConfiguration(value: unknown): ServingConfiguration {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalid("serving_configuration", "must be an object");
  const raw = value as Record<string, unknown>;
  const result: ServingConfiguration = {};
  for (const descriptor of VLLM_SERVING_PARAMETERS) result[descriptor.name] = descriptor.default;
  for (const [name, item] of Object.entries(raw)) {
    const descriptor = descriptorByName.get(name);
    if (!descriptor) throw new BittuneError("unsupported_serving_parameter", `vLLM Capability does not expose parameter ${name}.`, false);
    result[name] = assertDescriptorValue(descriptor, item);
  }
  const devices = result.gpu_device_ids as number[];
  const parallelWorkers = (result.tensor_parallel_size as number) * (result.pipeline_parallel_size as number);
  if (devices.length > 0 && devices.length < parallelWorkers) invalid("gpu_device_ids", "must contain at least tensor_parallel_size * pipeline_parallel_size devices");
  if ((result.max_num_batched_tokens as number) < (result.max_num_seqs as number)) invalid("max_num_batched_tokens", "must be >= max_num_seqs");
  return result;
}

export function vllmConfigurationFingerprint(configuration: ServingConfiguration): string {
  return sha256(canonicalJson({ capability_version: VLLM_CAPABILITY_VERSION, configuration }));
}

function usesNonDefault(configuration: ServingConfiguration, name: string): boolean {
  const descriptor = descriptorByName.get(name);
  return descriptor !== undefined && configuration[name] !== descriptor.default;
}

function appendSupportedArgument(args: string[], supportedFlags: Set<string> | undefined, flag: string, value: string, configuration: ServingConfiguration, parameter: string): void {
  if (!supportedFlags || supportedFlags.has(flag)) {
    args.push(flag, value);
    return;
  }
  if (usesNonDefault(configuration, parameter)) {
    throw new BittuneError("runtime_cli_incompatible", `The selected vLLM image does not support ${flag}, required by ${parameter}.`, false, { flag, parameter });
  }
}

export function compileVllmEngineArgs(configuration: ServingConfiguration, supportedFlags?: Iterable<string>): string[] {
  const config = materializeVllmConfiguration(configuration);
  const flags = supportedFlags ? new Set(supportedFlags) : undefined;
  const args: string[] = [];
  appendSupportedArgument(args, flags, "--dtype", String(config.dtype), config, "dtype");
  appendSupportedArgument(args, flags, "--tensor-parallel-size", String(config.tensor_parallel_size), config, "tensor_parallel_size");
  appendSupportedArgument(args, flags, "--pipeline-parallel-size", String(config.pipeline_parallel_size), config, "pipeline_parallel_size");
  appendSupportedArgument(args, flags, "--max-model-len", String(config.max_model_len), config, "max_model_len");
  appendSupportedArgument(args, flags, "--max-num-seqs", String(config.max_num_seqs), config, "max_num_seqs");
  appendSupportedArgument(args, flags, "--max-num-batched-tokens", String(config.max_num_batched_tokens), config, "max_num_batched_tokens");
  appendSupportedArgument(args, flags, "--gpu-memory-utilization", String(config.gpu_memory_utilization), config, "gpu_memory_utilization");
  appendSupportedArgument(args, flags, "--swap-space", String(config.swap_space_gib), config, "swap_space_gib");
  if (config.enforce_eager) {
    if (flags && !flags.has("--enforce-eager")) throw new BittuneError("runtime_cli_incompatible", "The selected vLLM image does not support --enforce-eager.", false, { flag: "--enforce-eager" });
    args.push("--enforce-eager");
  }
  if (config.enable_prefix_caching && (!flags || flags.has("--enable-prefix-caching"))) args.push("--enable-prefix-caching");
  return args;
}

export function compileDockerGpuArgs(configuration: ServingConfiguration): string[] {
  const devices = materializeVllmConfiguration(configuration).gpu_device_ids as number[];
  return ["--gpus", devices.length ? `device=${devices.join(",")}` : "all"];
}
