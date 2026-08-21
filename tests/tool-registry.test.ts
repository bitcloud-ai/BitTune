import assert from "node:assert/strict";
import { chmod, mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { delimiter, join } from "node:path";
import test from "node:test";
import type { ExtensionContext, ToolDefinition } from "../packages/bittune-runtime/src/core/extensions/types.ts";
import bittuneExtension, { createBittuneExtension } from "../packages/bittune-capabilities/src/index.ts";
import {
  CAPABILITY_ACTIVATION_TOOL,
  CAPABILITY_SESSION_ENTRY,
  CAPABILITY_TOOL_NAMES,
  CORE_TOOL_NAMES,
} from "../packages/bittune-capabilities/src/shared/capability-catalog.ts";
import { canonicalJson, sha256 } from "../packages/bittune-capabilities/src/shared/identifiers.ts";
import { StateStore, type ServiceManifest } from "../packages/bittune-capabilities/src/shared/state-store.ts";
import { VLLM_CAPABILITY_VERSION, compileVllmEngineArgs, materializeVllmConfiguration } from "../packages/bittune-capabilities/src/shared/vllm-capabilities.ts";
import { BITTUNE_SYSTEM_PROMPT, BITTUNE_SYSTEM_PROMPT_MARKER } from "../packages/bittune-capabilities/src/shared/system-prompt.ts";
import { createToolRegistry } from "../packages/bittune-capabilities/src/tool-registry.ts";

const expectedNames = [
  "inspect_gpu", "inspect_linux_host", "inspect_container_runtime",
  "list_inference_runtimes", "inspect_inference_runtime", "list_local_model_artifacts", "list_inference_services", "inspect_inference_service", "probe_inference_endpoint",
  "derive_deployment_options",
  "publish_experiment_spec", "list_experiment_specs", "get_experiment_spec", "record_experiment_trial", "list_experiment_trials", "get_experiment_trial", "derive_experiment_comparison", "publish_experiment_comparison", "list_experiment_comparisons", "get_experiment_comparison",
  "list_deployment_presets", "get_deployment_preset", "publish_deployment_preset",
  "list_capacity_baselines", "get_capacity_baseline", "derive_capacity_baseline", "publish_capacity_baseline",
  "inspect_vllm_image", "pull_vllm_image", "inspect_model_snapshot", "download_model_snapshot",
  "inspect_vllm_capabilities", "list_vllm_services", "start_vllm_service", "wait_for_vllm_ready", "read_vllm_service_logs", "inspect_vllm_service", "probe_vllm_endpoint", "stop_vllm_service",
  "run_performance_test", "analyze_benchmark_artifact", "list_run_records", "get_run_record", "read_artifact_excerpt",
];

const model = { model_id: "acme/meteor-8b", model_revision: "0123456789abcdef0123456789abcdef01234567", runtime_image_repository: "registry.example/acme/vllm", runtime_image_digest: "sha256:" + "a".repeat(64) };
const configuration = materializeVllmConfiguration({ max_model_len: 4096, max_num_seqs: 4, max_num_batched_tokens: 4096, gpu_memory_utilization: 0.8, gpu_device_ids: [0] });
const workload = { mode: "closed_loop" as const, concurrency: 2, request_count: 4, input_tokens: 64, output_tokens: 16, request_timeout_seconds: 60 };

async function invoke(root: string, name: string, params: Record<string, unknown>) {
  const tool = createToolRegistry(root).find((item) => item.name === name) as ToolDefinition;
  assert.ok(tool, `missing tool ${name}`);
  const result = await tool.execute("test-call", params as never, undefined, undefined, {} as ExtensionContext);
  const block = result.content.find((item) => item.type === "text");
  assert.ok(block && block.type === "text");
  const projection = JSON.parse(block.text) as { run_id?: string; status: "completed" | "failed" | "cancelled"; query_data?: Record<string, unknown> };
  if (!projection.run_id) {
    return { ok: projection.status === "completed", data: projection.query_data, run_id: "" };
  }
  const store = new StateStore(root);
  try {
    const { observation } = await store.getRun(projection.run_id);
    return { ok: observation.ok, data: observation.data as Record<string, unknown> | undefined, error: observation.error, run_id: projection.run_id };
  } finally {
    store.close();
  }
}

async function completedRun(store: StateStore, toolName: string, data: Record<string, unknown>, input: Record<string, unknown> = {}) {
  await store.initialize();
  const started = await store.startRun(toolName, input);
  const observation = { ok: true, summary: "fixture", provenance_type: "measured" as const, measured_at: new Date().toISOString(), run_id: started.run_id, data, warnings: [], artifacts: [] };
  await store.finishRun(started.run_id, { tool_name: toolName, started_at: started.started_at, status: "completed", provenance_type: "measured", input, artifacts: [], observation });
  return started.run_id;
}

async function publishGenericPreset(root: string) {
  const result = await invoke(root, "publish_deployment_preset", {
    preset_id: "meteor-vllm", model_id: model.model_id, model_revision: model.model_revision, runtime_kind: "vllm", runtime_image_repository: model.runtime_image_repository, runtime_image_digest: model.runtime_image_digest,
    serving_configuration: { max_model_len: 4096, max_num_seqs: 4, max_num_batched_tokens: 4096, gpu_memory_utilization: 0.8, gpu_device_ids: [0] },
  });
  assert.equal(result.ok, true, JSON.stringify(result));
  return result.data as { preset_id: string; version: string };
}

async function installExitedDockerFixture(root: string): Promise<{ bin: string; log: string }> {
  const bin = join(root, "fake-docker-bin");
  const log = join(root, "fake-docker-commands.jsonl");
  const driver = join(root, "fake-docker.mjs");
  await mkdir(bin, { recursive: true });
  await writeFile(driver, `import { appendFileSync } from "node:fs";
const args = process.argv.slice(2);
appendFileSync(process.env.BITTUNE_TEST_FAKE_DOCKER_LOG, JSON.stringify(args) + "\\n");
const print = (value) => process.stdout.write(value + "\\n");
if (args[0] === "image" && args[1] === "inspect") print(JSON.stringify({ Id: "sha256:${"a".repeat(64)}", RepoDigests: [] }));
else if (args[0] === "run" && args.includes("--help")) print("--model MODEL --served-model-name NAME --host HOST --port PORT --dtype DTYPE --tensor-parallel-size SIZE --pipeline-parallel-size SIZE --max-model-len LENGTH --max-num-seqs COUNT --max-num-batched-tokens COUNT --gpu-memory-utilization FRACTION --enable-prefix-caching");
else if (args[0] === "run" && args.includes("--version")) print("vLLM version 0.27.1");
else if (args[0] === "container" && args[1] === "ls") print("");
else if (args[0] === "run" && args.includes("-d")) print("${"b".repeat(64)}");
else if (args[0] === "container" && args[1] === "inspect") print(JSON.stringify({ Status: "exited", Running: false, StartedAt: "2026-08-19T00:00:00Z", FinishedAt: "2026-08-19T00:00:01Z", ExitCode: 2, OOMKilled: false, Error: "" }));
else if (args[0] === "container" && args[1] === "logs") print("vLLM failed while loading the model");
else { process.stderr.write("unexpected docker call: " + JSON.stringify(args)); process.exitCode = 1; }
`);
  if (process.platform === "win32") {
    await writeFile(join(bin, "docker.cmd"), `@echo off\r\n"${process.execPath}" "${driver}" %*\r\n`);
  } else {
    const shim = join(bin, "docker");
    await writeFile(shim, `#!/bin/sh\nexec ${JSON.stringify(process.execPath)} ${JSON.stringify(driver)} "$@"\n`);
    await chmod(shim, 0o755);
  }
  return { bin, log };
}

function serviceManifest(instanceId: string, port: number, servingConfiguration = configuration, owner?: ServiceManifest["owner"]): ServiceManifest {
  return {
    schema_version: 2, instance_id: instanceId, service_name: instanceId, container_id: (instanceId + "000000000000000000000000000000000000000000000000000000000000").slice(0, 12), container_name: `bittune-${instanceId}`,
    deployment_preset_id: "meteor-vllm", deployment_preset_version: "v1", model_id: model.model_id, model_revision: model.model_revision,
    runtime_kind: "vllm", runtime_image_repository: model.runtime_image_repository, runtime_image_digest: model.runtime_image_digest, runtime_capability_version: VLLM_CAPABILITY_VERSION,
    endpoint_url: `http://127.0.0.1:${port}`, port, serving_configuration: servingConfiguration,
    config_hash: sha256(canonicalJson({ instanceId, servingConfiguration })), created_at: new Date().toISOString(), last_known_status: "running", ...(owner ? { owner } : {}),
  };
}

test("registry exposes generic atomic tools", () => {
  const tools = createToolRegistry(join(tmpdir(), "bittune-registry-test"));
  assert.deepEqual(tools.map((tool) => tool.name), expectedNames);
  assert.equal(new Set(tools.map((tool) => tool.name)).size, expectedNames.length);
  assert.equal(tools.some((tool) => /qwen|5090/i.test(tool.name)), false);
});

test("vLLM engine arguments follow the selected image CLI profile", () => {
  const defaults = materializeVllmConfiguration({});
  const modernImageFlags = ["--model", "--served-model-name", "--host", "--port", "--dtype", "--tensor-parallel-size", "--pipeline-parallel-size", "--max-model-len", "--max-num-seqs", "--max-num-batched-tokens", "--gpu-memory-utilization", "--enable-prefix-caching"];
  const args = compileVllmEngineArgs(defaults, modernImageFlags);
  assert.equal(args.includes("--swap-space"), false);
  assert.equal(args.includes("--enable-prefix-caching"), true);

  const legacyImageFlags = [...modernImageFlags, "--swap-space", "--enforce-eager"];
  assert.equal(compileVllmEngineArgs(defaults, legacyImageFlags).includes("--swap-space"), true);
  assert.throws(() => compileVllmEngineArgs(materializeVllmConfiguration({ swap_space_gib: 8 }), modernImageFlags), (error: unknown) => (error as { stable?: { code?: string } }).stable?.code === "runtime_cli_incompatible");
  assert.throws(() => compileVllmEngineArgs(materializeVllmConfiguration({ enforce_eager: true }), modernImageFlags), (error: unknown) => (error as { stable?: { code?: string } }).stable?.code === "runtime_cli_incompatible");
});

test("serving fails fast with Docker evidence when a container exits before Ready", { skip: process.platform === "win32" ? "Docker CLI shims cannot be spawned without a native executable on Windows." : false }, async () => {
  const root = await mkdtemp(join(tmpdir(), "bittune-serving-docker-"));
  const cacheRoot = join(root, "cache");
  const previousPath = process.env.PATH;
  const previousCacheRoots = process.env.BITTUNE_MODEL_CACHE_ROOTS;
  const previousDockerLog = process.env.BITTUNE_TEST_FAKE_DOCKER_LOG;
  try {
    const docker = await installExitedDockerFixture(root);
    process.env.PATH = `${docker.bin}${delimiter}${previousPath ?? ""}`;
    process.env.BITTUNE_MODEL_CACHE_ROOTS = cacheRoot;
    process.env.BITTUNE_TEST_FAKE_DOCKER_LOG = docker.log;
    const snapshot = join(cacheRoot, "hub", "models--acme--meteor-8b", "snapshots", model.model_revision);
    await mkdir(snapshot, { recursive: true });
    await writeFile(join(snapshot, "model.safetensors"), "fixture");
    await publishGenericPreset(root);

    const started = await invoke(root, "start_vllm_service", { deployment_preset_id: "meteor-vllm", service_name: "meteor", port: 18080 });
    assert.equal(started.ok, false);
    assert.equal(started.error?.code, "container_start_failed", await readFile(docker.log, "utf8"));
    const store = new StateStore(root);
    const [service] = await store.listServices();
    assert.ok(service);
    assert.equal(service.last_known_status, "exited");
    assert.equal(service.runtime_cli_profile?.version, "0.27.1");
    assert.equal(service.runtime_engine_args?.includes("--swap-space"), false);
    const { observation } = await store.getRun(started.run_id);
    assert.equal(observation.artifacts.some((artifact) => artifact.label === "docker-start-failure-logs"), true);

    const readyStarted = Date.now();
    const ready = await invoke(root, "wait_for_vllm_ready", { instance_id: service.instance_id, timeout_seconds: 30 });
    assert.equal(ready.ok, true);
    assert.equal(ready.data?.ready, false);
    assert.equal(ready.data?.failure_kind, "container_not_running");
    assert.ok(Date.now() - readyStarted < 2_000);
    const dockerCalls = (await readFile(docker.log, "utf8")).trim().split("\n").filter(Boolean).map((line) => JSON.parse(line) as string[]);
    const launch = dockerCalls.find((args) => args[0] === "run" && args.includes("-d"));
    assert.ok(launch);
    assert.equal(launch.includes("--swap-space"), false);
    store.close();
  } finally {
    if (previousPath === undefined) delete process.env.PATH; else process.env.PATH = previousPath;
    if (previousCacheRoots === undefined) delete process.env.BITTUNE_MODEL_CACHE_ROOTS; else process.env.BITTUNE_MODEL_CACHE_ROOTS = previousCacheRoots;
    if (previousDockerLog === undefined) delete process.env.BITTUNE_TEST_FAKE_DOCKER_LOG; else process.env.BITTUNE_TEST_FAKE_DOCKER_LOG = previousDockerLog;
    await rm(root, { recursive: true, force: true });
  }
});

test("static capability entry registers the generic tool registry", async () => {
  const registered: string[] = [];
  bittuneExtension({ registerTool: (tool: ToolDefinition) => registered.push(tool.name), on: () => undefined } as never);
  assert.deepEqual(registered, [...expectedNames, CAPABILITY_ACTIVATION_TOOL]);
});

test("unknown Bash changes invalidate managed service observations without creating a Run", async () => {
  const root = await mkdtemp(join(tmpdir(), "bittune-bash-state-"));
  let store: StateStore | undefined;
  try {
    store = new StateStore(root);
    const service = { ...serviceManifest("service-bash-state", 18002), observation: { desired_state: "running" as const, observed_state: "running", observed_at: new Date().toISOString(), freshness: "fresh" as const, state_generation: 1 } };
    await store.saveService(service);
    let toolResult: ((event: unknown) => Promise<unknown>) | undefined;
    createBittuneExtension({ stateRoot: root })({
      registerTool: () => undefined,
      setActiveTools: () => undefined,
      appendEntry: () => undefined,
      on: (event: string, handler: (event: unknown) => Promise<unknown>) => { if (event === "tool_result") toolResult = handler; },
    } as never);
    assert.ok(toolResult);
    await toolResult!({ type: "tool_result", toolName: "bash", toolCallId: "readonly", input: { command: "nvidia-smi" }, content: [], details: undefined, isError: false });
    assert.equal((await store.getService(service.instance_id)).observation?.freshness, "fresh");
    await toolResult!({ type: "tool_result", toolName: "bash", toolCallId: "mutating", input: { command: "docker container stop external-service" }, content: [], details: undefined, isError: false });
    assert.equal((await store.getService(service.instance_id)).observation?.freshness, "stale");
    assert.deepEqual(await store.listRuns({ tool_name: "bash" }), []);
  } finally {
    store?.close();
    await rm(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
});

test("canonical system prompt is tool-set independent and defers availability to Tool definitions", () => {
  assert.equal(BITTUNE_SYSTEM_PROMPT.includes(BITTUNE_SYSTEM_PROMPT_MARKER), true);
  assert.match(BITTUNE_SYSTEM_PROMPT, /当前 Tool definitions 中出现的 Tool/);
  assert.doesNotMatch(BITTUNE_SYSTEM_PROMPT, /Session Capability Catalog/);
});

test("capability activation expands only the static catalog and restores from Session entries", async () => {
  const root = await mkdtemp(join(tmpdir(), "bittune-capability-catalog-"));
  try {
    type SessionStartHandler = (event: unknown, context: { sessionManager: { getBranch: () => readonly unknown[] } }) => Promise<void>;
    const registered = new Map<string, ToolDefinition>();
    const activeToolSets: string[][] = [];
    const entries: Array<{ type: "custom"; customType: string; data: unknown }> = [];
    let sessionStart: SessionStartHandler | undefined;
    bittuneExtension({
      registerTool: (tool: ToolDefinition) => registered.set(tool.name, tool),
      setActiveTools: (names: string[]) => activeToolSets.push(names),
      appendEntry: (customType: string, data: unknown) => entries.push({ type: "custom", customType, data }),
      on: (event: string, handler: SessionStartHandler) => {
        if (event === "session_start") sessionStart = handler;
      },
    } as never);

    assert.ok(sessionStart);
    await sessionStart!({ type: "session_start" }, { sessionManager: { getBranch: () => entries } });
    assert.deepEqual(activeToolSets.at(-1), [...CORE_TOOL_NAMES]);

    const activation = registered.get(CAPABILITY_ACTIVATION_TOOL);
    assert.ok(activation);
    const result = await activation.execute("activate-benchmark", { capability: "benchmark" }, undefined, undefined, {} as ExtensionContext);
    const block = result.content.find((item) => item.type === "text");
    assert.ok(block && block.type === "text");
    const observation = JSON.parse(block.text) as { status: string; query_data: { active_tool_names: string[] } };
    assert.equal(observation.status, "completed");
    assert.deepEqual(entries.map((entry) => entry.customType), [CAPABILITY_SESSION_ENTRY]);
    assert.deepEqual(activeToolSets.at(-1), [...CORE_TOOL_NAMES, ...CAPABILITY_TOOL_NAMES.benchmark]);
    assert.deepEqual(observation.query_data.active_tool_names, [...CORE_TOOL_NAMES, ...CAPABILITY_TOOL_NAMES.benchmark]);

    const restoredSets: string[][] = [];
    let restoredSessionStart: SessionStartHandler | undefined;
    createBittuneExtension({ initialCapabilities: ["benchmark"] })({
      registerTool: () => undefined,
      setActiveTools: (names: string[]) => restoredSets.push(names),
      appendEntry: () => undefined,
      on: (event: string, handler: SessionStartHandler) => {
        if (event === "session_start") restoredSessionStart = handler;
      },
    } as never);
    assert.ok(restoredSessionStart);
    await restoredSessionStart!({ type: "session_start" }, { sessionManager: { getBranch: () => entries } });
    assert.deepEqual(restoredSets.at(-1), [...CORE_TOOL_NAMES, ...CAPABILITY_TOOL_NAMES.benchmark]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("generic DeploymentPreset persists finite configuration and rejects unknown settings", async () => {
  const root = await mkdtemp(join(tmpdir(), "bittune-preset-"));
  try {
    const listed = await invoke(root, "list_deployment_presets", {});
    assert.deepEqual(listed.data?.presets, []);
    const published = await publishGenericPreset(root);
    assert.equal(published.version, "v1");
    const preset = await invoke(root, "get_deployment_preset", { preset_id: "meteor-vllm" });
    assert.equal(preset.data?.model_id, model.model_id);
    assert.equal((preset.data?.serving_configuration as Record<string, unknown>).max_num_seqs, 4);
    const invalid = await invoke(root, "publish_deployment_preset", { preset_id: "unsafe", model_id: "acme/test", model_revision: model.model_revision, runtime_kind: "vllm", runtime_image_repository: model.runtime_image_repository, runtime_image_digest: model.runtime_image_digest, serving_configuration: { arbitrary_engine_arg: "--unsafe" } });
    assert.equal(invalid.ok, false);
    assert.equal(invalid.error?.code, "unsupported_serving_parameter");
    const multiGpu = await invoke(root, "publish_deployment_preset", { preset_id: "many-gpu", model_id: "acme/many-gpu", model_revision: "main", runtime_kind: "vllm", runtime_image_repository: "registry.example:5000/acme/vllm", runtime_image_digest: model.runtime_image_digest, serving_configuration: { tensor_parallel_size: 12, gpu_device_ids: Array.from({ length: 12 }, (_, index) => index), max_model_len: 524288, max_num_batched_tokens: 524288 } });
    assert.equal(multiGpu.ok, true, JSON.stringify(multiGpu));
  } finally { await rm(root, { recursive: true, force: true }); }
});



test("Capability is explicit and local model discovery parses only allowed metadata", async () => {
  const root = await mkdtemp(join(tmpdir(), "bittune-discovery-"));
  const cacheRoot = join(root, "cache"); const previous = process.env.BITTUNE_MODEL_CACHE_ROOTS; process.env.BITTUNE_MODEL_CACHE_ROOTS = cacheRoot;
  try {
    const snapshot = join(cacheRoot, "hub", "models--acme--meteor-8b", "snapshots", model.model_revision);
    await mkdir(snapshot, { recursive: true }); await writeFile(join(snapshot, "model.safetensors"), "fixture");
    await writeFile(join(snapshot, "config.json"), JSON.stringify({ architectures: ["MeteorForCausalLM"], torch_dtype: "bfloat16", max_position_embeddings: 32768, quantization_config: { quant_method: "awq" } }));
    await writeFile(join(snapshot, "tokenizer_config.json"), JSON.stringify({ tokenizer_class: "MeteorTokenizer", chat_template: "{{ messages }}", token: "must-not-leak" }));
    await writeFile(join(snapshot, "generation_config.json"), JSON.stringify({ max_new_tokens: 512 })); await writeFile(join(snapshot, "model.safetensors.index.json"), JSON.stringify({ weight_map: { "a": "model.safetensors" } }));
    const artifacts = await invoke(root, "list_local_model_artifacts", {}); const artifact = (artifacts.data?.artifacts as Array<Record<string, unknown>>)[0]!;
    assert.equal(artifact.model_id, model.model_id); assert.equal((artifact.metadata as Record<string, unknown>).architecture, "MeteorForCausalLM"); assert.equal((artifact.metadata as Record<string, unknown>).quantization, "awq"); assert.equal((artifact.metadata as Record<string, unknown>).chat_template_available, true); assert.equal(JSON.stringify(artifact).includes("must-not-leak"), false); assert.match(String(artifact.metadata_fingerprint), /^sha256:/);
    const preset = await publishGenericPreset(root);
    const snapshotStore = new StateStore(root);
    const resolved = await snapshotStore.resolveModelSnapshot({
      schema_version: 2, preset_id: preset.preset_id, version: preset.version, model_id: model.model_id, model_revision: model.model_revision,
      runtime_kind: "vllm", runtime_image_repository: model.runtime_image_repository, runtime_image_digest: model.runtime_image_digest,
      serving_configuration: configuration, source_run_ids: [], created_at: new Date().toISOString(), source: "published", content_hash: "",
    });
    snapshotStore.close();
    assert.deepEqual(resolved, { cache_root: cacheRoot, host_snapshot_path: snapshot });
    const capability = await invoke(root, "inspect_vllm_capabilities", {}); assert.equal(capability.ok, true); assert.equal(capability.data?.capability_version, VLLM_CAPABILITY_VERSION); assert.ok(Array.isArray(capability.data?.parameters));
  } finally { if (previous === undefined) delete process.env.BITTUNE_MODEL_CACHE_ROOTS; else process.env.BITTUNE_MODEL_CACHE_ROOTS = previous; await rm(root, { recursive: true, force: true }); }
});

test("capacity derivation publishes an evidence-backed operating point without a maximum claim", async () => {
  const root = await mkdtemp(join(tmpdir(), "bittune-capacity-"));
  try {
    await publishGenericPreset(root); const store = new StateStore(root); const service = serviceManifest("service-reference", 18000); await store.saveService(service);
    const environment = { host_id: "gpu-fixture", gpus: [{ uuid: "GPU-any", name: "Any NVIDIA GPU", driver_version: "600.0", compute_capability: "9.0", memory_total_bytes: 48 * 1024 ** 3 }], compute_processes: [] };
    const envRun = await completedRun(store, "inspect_gpu", environment); const serviceRun = await completedRun(store, "inspect_vllm_service", { service: { ...service, current_container_state: { status: "running", running: true } } }); const probeRun = await completedRun(store, "probe_vllm_endpoint", { instance_id: service.instance_id, response_model_id: model.model_id }); const benchmarkRun = await completedRun(store, "run_performance_test", { service_instance_id: service.instance_id, requested: workload, metrics: { success_rate: 1, requests_per_second: 18 }, observed_gpu_memory_peak_bytes: 12 * 1024 ** 3 });
    const candidate = await invoke(root, "derive_capacity_baseline", { deployment_preset_id: "meteor-vllm", source_run_ids: [envRun, serviceRun, probeRun, benchmarkRun] }); assert.equal(candidate.ok, true, JSON.stringify(candidate)); const operatingPoint = (candidate.data?.operating_points as Array<Record<string, unknown>> | undefined)?.[0]; assert.equal(operatingPoint?.verification_status, "verified");
    const published = await invoke(root, "publish_capacity_baseline", { candidate_run_id: candidate.run_id, candidate_hash: candidate.data?.candidate_hash }); assert.equal(published.ok, true, JSON.stringify(published)); assert.equal((published.data?.operating_points as Array<unknown>).length, 1); assert.equal("validated_max_context_tokens" in (published.data ?? {}), false);
    const duplicateEnvironment = await completedRun(store, "inspect_gpu", environment); const duplicateEvidence = await invoke(root, "derive_capacity_baseline", { deployment_preset_id: "meteor-vllm", source_run_ids: [envRun, duplicateEnvironment, serviceRun, probeRun, benchmarkRun] }); assert.equal(duplicateEvidence.ok, false); assert.equal(duplicateEvidence.error?.code, "evidence_conflict");
    const insufficientBenchmark = await completedRun(store, "run_performance_test", { service_instance_id: service.instance_id, requested: workload, metrics: { success_rate: 0.9, requests_per_second: 18 } }); const insufficient = await invoke(root, "derive_capacity_baseline", { deployment_preset_id: "meteor-vllm", source_run_ids: [envRun, serviceRun, probeRun, insufficientBenchmark] }); assert.equal(insufficient.data?.operating_points && (insufficient.data.operating_points as Array<Record<string, unknown>>)[0]?.verification_status, "validation_required"); const rejected = await invoke(root, "publish_capacity_baseline", { candidate_run_id: insufficient.run_id, candidate_hash: insufficient.data?.candidate_hash }); assert.equal(rejected.ok, false); assert.equal(rejected.error?.code, "evidence_incomplete"); store.close();
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("experiment comparison requires repeated reference evidence and protects owned candidate services", async () => {
  const root = await mkdtemp(join(tmpdir(), "bittune-experiment-"));
  let store: StateStore | undefined;
  try {
    await publishGenericPreset(root); store = new StateStore(root); const activeStore = store; const reference = serviceManifest("service-reference", 18000); const candidateConfiguration = materializeVllmConfiguration({ ...configuration, max_num_seqs: 8 }); const candidate = serviceManifest("service-candidate", 18001, candidateConfiguration, { kind: "experiment", experiment_id: "tuning", experiment_version: "v1" }); await activeStore.saveService(reference); await activeStore.saveService(candidate);
    const environment = { host_id: "gpu-fixture", gpus: [{ uuid: "GPU-any", name: "Any GPU", driver_version: "600.0", compute_capability: "9.0", memory_total_bytes: 48 * 1024 ** 3 }], compute_processes: [] as Array<{ gpu_uuid: string; pid: number; process_name: string; memory_bytes: number }> }; const baseline = await completedRun(activeStore, "inspect_gpu", environment);
    const spec = await invoke(root, "publish_experiment_spec", { experiment_id: "tuning", experiment_kind: "runtime_tuning", deployment_preset_id: "meteor-vllm", deployment_preset_version: "v1", reference_service_instance_id: reference.instance_id, baseline_environment_run_id: baseline, objective_metric: "requests_per_second", constraints: { min_success_rate: 0.95 }, workload, allowed_parameters: { max_num_seqs: { minimum: 4, maximum: 8 } }, budget: { max_trials: 5, max_total_duration_seconds: 600, max_failures: 2 }, minimum_improvement_percent: 5, required_repetitions: 2 }); assert.equal(spec.ok, true, JSON.stringify(spec));
    async function record(service: ServiceManifest, rps: number, configurationInput: Record<string, unknown>, preEnvironment = environment, postEnvironment = environment) {
      const pre = await completedRun(activeStore, "inspect_gpu", preEnvironment); const post = await completedRun(activeStore, "inspect_gpu", postEnvironment); const serviceRun = await completedRun(activeStore, "inspect_vllm_service", { service: { ...service, current_container_state: { status: "running" } } }); const probe = await completedRun(activeStore, "probe_vllm_endpoint", { instance_id: service.instance_id, response_model_id: model.model_id }); const benchmark = await completedRun(activeStore, "run_performance_test", { service_instance_id: service.instance_id, requested: workload, metrics: { success_rate: 1, requests_per_second: rps }, observed_gpu_memory_peak_bytes: 10 * 1024 ** 3 }); return invoke(root, "record_experiment_trial", { experiment_id: "tuning", serving_configuration: configurationInput, workload, pre_environment_run_id: pre, post_environment_run_id: post, service_run_id: serviceRun, probe_run_id: probe, benchmark_run_id: benchmark });
    }
    assert.equal((await record(reference, 10, {})).data?.result_class, "valid"); const beforeReferenceRepeat = await invoke(root, "derive_experiment_comparison", { experiment_id: "tuning" }); assert.equal(beforeReferenceRepeat.data?.status, "validation_required");
    assert.equal((await record(reference, 10, {})).data?.result_class, "valid"); assert.equal((await record(candidate, 12, { max_num_seqs: 8 })).data?.result_class, "valid"); assert.equal((await record(candidate, 12, { max_num_seqs: 8 })).data?.result_class, "valid"); const comparison = await invoke(root, "derive_experiment_comparison", { experiment_id: "tuning" }); assert.equal(comparison.data?.status, "objective_met");
    const competingEnvironment = { ...environment, compute_processes: [{ gpu_uuid: "GPU-any", pid: 9001, process_name: "external-worker", memory_bytes: 1024 }] }; const contention = await record(reference, 10, {}, environment, competingEnvironment); assert.equal(contention.data?.result_class, "invalid_resource_contention");
  } finally { store?.close(); await rm(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 }); }
});

test("base distribution is runtime-free and does not mutate GPU providers", async () => {
  const [build, installer, manifest] = await Promise.all([readFile("install/build-offline-bundle-ubuntu.sh", "utf8"), readFile("install/install-ubuntu.sh", "utf8"), readFile("install/offline-manifest.env", "utf8")]);
  assert.match(build, /runtime_selection=runtime-free/);
  assert.doesNotMatch(installer, /nvidia-ctk|docker-ce|systemctl restart docker/);
  assert.doesNotMatch(build, /docker pull|nvidia-container-toolkit/);
  assert.doesNotMatch(manifest, /^VLLM_IMAGE=/m);
});
