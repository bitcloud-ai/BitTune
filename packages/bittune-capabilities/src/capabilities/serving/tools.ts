import { Type } from "typebox";
import { relative } from "node:path";
import { BittuneError } from "../../shared/errors.ts";
import { canonicalJson, newId, sha256 } from "../../shared/identifiers.ts";
import { requireSuccess, runCommand } from "../../shared/process.ts";
import { RunRecorder } from "../../shared/run-recorder.ts";
import type { DeploymentPreset, ServiceManifest } from "../../shared/state-store.ts";
import { createBittuneTool } from "../../shared/tool.ts";
import { compileDockerGpuArgs, compileVllmEngineArgs, materializeVllmConfiguration, vllmCapabilitySnapshot, VLLM_CAPABILITY_VERSION, type ServingConfiguration } from "../../shared/vllm-capabilities.ts";
import { inspectVllmCliProfile } from "../../shared/vllm-cli.ts";
import { resolveVllmImage } from "../../shared/vllm-image.ts";

const Id = Type.String({ pattern: "^[a-z][a-z0-9-]{0,62}$" });
const Version = Type.String({ pattern: "^v[1-9][0-9]*$" });
const Configuration = Type.Partial(Type.Object({
  dtype: Type.Union([Type.Literal("auto"), Type.Literal("bfloat16"), Type.Literal("float16"), Type.Literal("float32")]), tensor_parallel_size: Type.Integer({ minimum: 1 }), pipeline_parallel_size: Type.Integer({ minimum: 1 }),
  gpu_device_ids: Type.Array(Type.Integer({ minimum: 0 })), max_model_len: Type.Integer({ minimum: 1 }), max_num_seqs: Type.Integer({ minimum: 1 }), max_num_batched_tokens: Type.Integer({ minimum: 1 }),
  gpu_memory_utilization: Type.Number({ exclusiveMinimum: 0, maximum: 1 }), swap_space_gib: Type.Number({ minimum: 0 }), enforce_eager: Type.Boolean(), enable_prefix_caching: Type.Boolean(),
}));

interface DockerServiceState {
  status: string;
  running: boolean;
  started_at?: string;
  finished_at?: string;
  exit_code?: number;
  oom_killed?: boolean;
  error?: string;
}

async function dockerServiceState(manifest: ServiceManifest, signal?: AbortSignal): Promise<DockerServiceState> {
  const result = await runCommand("docker", ["container", "inspect", manifest.container_id, "--format", "{{json .State}}"], { signal, timeoutMs: 15_000 });
  if (result.exit_code !== 0) return { status: "not_found", running: false, error: result.stderr.slice(0, 500) };
  const state = JSON.parse(result.stdout) as { Status?: string; Running?: boolean; StartedAt?: string; FinishedAt?: string; ExitCode?: number; OOMKilled?: boolean; Error?: string };
  return {
    status: state.Status ?? "unknown",
    running: Boolean(state.Running),
    ...(state.StartedAt ? { started_at: state.StartedAt } : {}),
    ...(state.FinishedAt ? { finished_at: state.FinishedAt } : {}),
    ...(state.ExitCode !== undefined ? { exit_code: state.ExitCode } : {}),
    ...(state.OOMKilled !== undefined ? { oom_killed: state.OOMKilled } : {}),
    ...(state.Error ? { error: state.Error } : {}),
  };
}

function manifestData(manifest: ServiceManifest, state: unknown) { return { service: { ...manifest, current_container_state: state } }; }
function tailUtf8(value: string, maxBytes: number): string { const bytes = Buffer.from(value, "utf8"); return bytes.length <= maxBytes ? value : bytes.subarray(bytes.length - maxBytes).toString("utf8"); }

async function modelPathInContainer(snapshot: { cache_root: string; host_snapshot_path: string }): Promise<string> {
  const pathWithinCache = relative(snapshot.cache_root, snapshot.host_snapshot_path);
  if (!pathWithinCache || pathWithinCache === ".." || pathWithinCache.startsWith("..\\") || pathWithinCache.startsWith("../")) throw new BittuneError("provider_error", "Model snapshot is outside the configured cache root.", false);
  return `/root/.cache/huggingface/${pathWithinCache.replaceAll("\\", "/")}`;
}

function endpointUrl(manifest: ServiceManifest, path: string): string { return `${manifest.endpoint_url}${path}`; }

async function readDockerLogs(manifest: ServiceManifest, signal?: AbortSignal, tailLines = 200): Promise<{ raw: string; available: boolean }> {
  const logs = await runCommand("docker", ["container", "logs", "--timestamps", "--tail", String(tailLines), manifest.container_id], { signal, timeoutMs: 15_000, maxBytes: 1024 * 1024 });
  const raw = `${logs.stdout}${logs.stderr ? `\n[stderr]\n${logs.stderr}` : ""}`;
  return { raw, available: logs.exit_code === 0 };
}

async function sleepWithAbort(milliseconds: number, signal?: AbortSignal): Promise<void> {
  if (!signal) {
    await new Promise<void>((resolve) => setTimeout(resolve, milliseconds));
    return;
  }
  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(done, milliseconds);
    const onAbort = () => { clearTimeout(timer); reject(new BittuneError("cancelled", "Ready wait cancelled.", true)); };
    function done() { signal?.removeEventListener("abort", onAbort); resolve(); }
    if (signal.aborted) return onAbort();
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

export function createServingTools(recorder: RunRecorder) {
  return [
    createBittuneTool({
      name: "inspect_vllm_capabilities", label: "读取 vLLM 能力", recorder, parameters: Type.Object({ deployment_preset_id: Type.Optional(Id), deployment_preset_version: Type.Optional(Version) }),
      description: "只读返回当前受管 vLLM Adapter 支持的有限 serving_configuration 参数、类型、默认值、范围和组合约束。Agent 应先用它判断候选；不暴露任意 vLLM flag。",
      async execute(params, context) {
        const input = params as { deployment_preset_id?: string; deployment_preset_version?: string };
        const runtime = input.deployment_preset_id ? await context.store.getPreset(input.deployment_preset_id, input.deployment_preset_version) : undefined;
        return { summary: `vLLM Capability ${VLLM_CAPABILITY_VERSION} 可用。`, provenance_type: "measured", data: vllmCapabilitySnapshot(runtime ? { image_repository: runtime.runtime_image_repository, image_digest: runtime.runtime_image_digest } : undefined), provider: { name: "bittune-vllm-adapter", version: VLLM_CAPABILITY_VERSION } };
      },
    }),
    createBittuneTool({
      name: "list_vllm_services", label: "列出受管 vLLM 服务", recorder, parameters: Type.Object({}),
      description: "读取 Bittune Service Manifest 并核对 Docker 当前状态。只读，不发送推理请求，不启动或停止服务。",
      async execute(_params, context) {
        const manifests = await context.store.listServices();
        const services = await Promise.all(manifests.map(async (manifest) => {
          const currentContainerState = await dockerServiceState(manifest, context.signal);
          const observed = await context.store.recordServiceObservation(manifest.instance_id, currentContainerState.status, context.run_id);
          return { ...observed, current_container_state: currentContainerState };
        }));
        return { summary: `发现 ${services.length} 个 Bittune 受管 vLLM 服务。`, provenance_type: "measured", data: { services }, provider: { name: "docker-cli" } };
      },
    }),
    createBittuneTool({
      name: "start_vllm_service", label: "启动 vLLM 服务", recorder,
      parameters: Type.Object({ deployment_preset_id: Id, deployment_preset_version: Type.Optional(Version), service_name: Type.String({ pattern: "^[a-z][a-z0-9-]{0,40}$" }), port: Type.Integer({ minimum: 1024, maximum: 65535 }), serving_configuration: Type.Optional(Configuration), owner: Type.Optional(Type.Object({ experiment_id: Id, experiment_version: Version })) }),
      description: "使用所选 DeploymentPreset 的本地 Runtime 镜像和模型 Snapshot 创建一个 Bittune 受管 vLLM 服务。只接受 Capability 声明的有限配置；不拉镜像、不下载模型、不等待 Ready、不调用其他 Tool。",
      async execute(params, context) {
        const input = params as { deployment_preset_id: string; deployment_preset_version?: string; service_name: string; port: number; serving_configuration?: ServingConfiguration; owner?: { experiment_id: string; experiment_version: string } };
        const preset = await context.store.getPreset(input.deployment_preset_id, input.deployment_preset_version);
        const imageReference = `${preset.runtime_image_repository}@${preset.runtime_image_digest}`;
        const image = await resolveVllmImage({ repository: preset.runtime_image_repository, digest: preset.runtime_image_digest }, context.signal);
        if (!image.image) throw new BittuneError("image_missing", `Runtime image ${imageReference} is not available locally.`, false);
        const snapshot = await context.store.resolveModelSnapshot(preset);
        if (!snapshot) throw new BittuneError("model_snapshot_missing", `Model snapshot ${preset.model_id}@${preset.model_revision} is not available locally.`, false);
        const snapshotPath = await modelPathInContainer(snapshot);
        const cliProfile = await inspectVllmCliProfile({ image_reference: image.image.image_reference, run_reference: image.image.run_reference }, context.signal);
        await context.artifact("vllm-cli-profile", cliProfile.diagnostics);
        const existingPort = await runCommand("docker", ["container", "ls", "--filter", `publish=${input.port}`, "--format", "{{.ID}}"], { signal: context.signal, timeoutMs: 15_000 });
        requireSuccess(existingPort);
        if (existingPort.stdout.trim()) throw new BittuneError("port_conflict", `Port ${input.port} is already used by a Docker container.`, false);
        const instanceId = newId("service"); const containerName = `bittune-${instanceId}`;
        const configuration = materializeVllmConfiguration({ ...preset.serving_configuration, ...(input.serving_configuration ?? {}) });
        const engineArgs = compileVllmEngineArgs(configuration, cliProfile.flags);
        const args = ["run", "-d", "--pull=never", "--name", containerName, ...compileDockerGpuArgs(configuration), "--ipc", "host", "-p", `127.0.0.1:${input.port}:8000`, "-v", `${snapshot.cache_root}:/root/.cache/huggingface:ro`, "--env", "HF_HUB_OFFLINE=1", "--env", "TRANSFORMERS_OFFLINE=1", "--env", "HF_DATASETS_OFFLINE=1", "--env", "HF_HUB_DISABLE_TELEMETRY=1", image.image.run_reference, "--model", snapshotPath, "--served-model-name", preset.model_id, ...engineArgs, "--host", "0.0.0.0", "--port", "8000"];
        context.update(`正在启动受管 vLLM 容器 ${containerName}…`);
        const started = requireSuccess(await runCommand("docker", args, { signal: context.signal, timeoutMs: 60_000, maxBytes: 1024 * 1024 }));
        await context.artifact("docker-run", `${started.stdout}\n${started.stderr}`);
        const containerId = started.stdout.trim();
        if (!/^[a-f0-9]{12,64}$/.test(containerId)) throw new BittuneError("provider_error", "Docker did not return a valid container ID.", true);
        const manifest: ServiceManifest = {
          schema_version: 2, instance_id: instanceId, service_name: input.service_name, container_id: containerId, container_name: containerName,
          deployment_preset_id: preset.preset_id, deployment_preset_version: preset.version, model_id: preset.model_id, model_revision: preset.model_revision,
          runtime_kind: "vllm", runtime_image_repository: preset.runtime_image_repository, runtime_image_digest: preset.runtime_image_digest, runtime_capability_version: VLLM_CAPABILITY_VERSION,
          runtime_cli_profile: { ...(cliProfile.version ? { version: cliProfile.version } : {}), supported_flags: cliProfile.flags, supports_swap_space: cliProfile.supports_swap_space }, runtime_engine_args: engineArgs,
          endpoint_url: `http://127.0.0.1:${input.port}`, port: input.port, serving_configuration: configuration,
          config_hash: sha256(canonicalJson({ preset_id: preset.preset_id, preset_version: preset.version, capability_version: VLLM_CAPABILITY_VERSION, serving_configuration: configuration })), created_at: new Date().toISOString(), last_known_status: "created",
          ...(input.owner ? { owner: { kind: "experiment", ...input.owner } } : {}),
        };
        const currentState = await dockerServiceState(manifest, context.signal);
        const stored = { ...manifest, last_known_status: currentState.status };
        await context.store.saveService(stored);
        const observed = await context.store.recordServiceObservation(instanceId, currentState.status, context.run_id, "running");
        if (!currentState.running) {
          const logs = await readDockerLogs(observed, context.signal);
          const artifact = await context.artifact("docker-start-failure-logs", logs.raw);
          throw new BittuneError("container_start_failed", `Managed vLLM container ${instanceId} did not remain running.`, false, {
            instance_id: instanceId,
            container_state: currentState,
            log_artifact_id: artifact.artifact_id,
            log_excerpt: tailUtf8(logs.raw, 8 * 1024),
          });
        }
        return { summary: `已启动受管 vLLM 服务 ${instanceId}，等待 Ready 需单独调用 wait_for_vllm_ready。`, provenance_type: "measured", data: manifestData(observed, currentState), provider: { name: "docker-cli", ...(cliProfile.version ? { version: cliProfile.version } : {}) } };
      },
    }),
    createBittuneTool({
      name: "wait_for_vllm_ready", label: "等待 vLLM Ready", recorder,
      parameters: Type.Object({ instance_id: Id, timeout_seconds: Type.Integer({ minimum: 1, maximum: 600 }), poll_interval_seconds: Type.Optional(Type.Number({ minimum: 0.1, maximum: 30 })) }),
      description: "对 Bittune 受管服务做有界的 /v1/models Ready 等待。只读取指定服务端点，不启动、修改或停止服务；超时返回 validation_required。",
      async execute(params, context) {
        const input = params as { instance_id: string; timeout_seconds: number; poll_interval_seconds?: number };
        const manifest = await context.store.getService(input.instance_id); const deadline = Date.now() + input.timeout_seconds * 1000; const interval = (input.poll_interval_seconds ?? 1) * 1000; let lastError = "";
        while (Date.now() < deadline) {
          const state = await dockerServiceState(manifest, context.signal);
          if (!state.running) {
            await context.store.recordServiceObservation(manifest.instance_id, state.status, context.run_id, "running");
            const logs = await readDockerLogs(manifest, context.signal);
            const artifact = await context.artifact("docker-ready-failure-logs", logs.raw);
            return {
              summary: `服务 ${manifest.instance_id} 在 Ready 前已停止。`,
              provenance_type: "measured",
              data: { instance_id: manifest.instance_id, ready: false, failure_kind: "container_not_running", container_state: state, log_excerpt: tailUtf8(logs.raw, 8 * 1024), log_artifact_id: artifact.artifact_id },
              warnings: ["容器未运行；请依据 Docker State 和日志分析原因，不要在未改变前提时重复等待。"],
              provider: { name: "docker-cli" },
            };
          }
          try {
            const remaining = Math.max(1, deadline - Date.now());
            const timeout = AbortSignal.timeout(remaining);
            const signal = context.signal ? AbortSignal.any([context.signal, timeout]) : timeout;
            const response = await fetch(endpointUrl(manifest, "/v1/models"), { signal, headers: { accept: "application/json" } });
            const body = await response.text(); if (response.ok) { await context.store.recordServiceObservation(manifest.instance_id, "ready", context.run_id, "running"); return { summary: `服务 ${manifest.instance_id} 已 Ready。`, provenance_type: "measured", data: { instance_id: manifest.instance_id, ready: true, status_code: response.status, elapsed_ms: input.timeout_seconds * 1000 - Math.max(0, deadline - Date.now()) }, provider: { name: "openai-compatible-http" } }; }
            lastError = `HTTP ${response.status}: ${body.slice(0, 200)}`;
          } catch (error) { if (context.signal?.aborted) throw new BittuneError("cancelled", "Ready wait cancelled.", true); lastError = error instanceof Error ? error.message : String(error); }
          await sleepWithAbort(Math.min(interval, Math.max(10, deadline - Date.now())), context.signal);
        }
        await context.store.recordServiceObservation(manifest.instance_id, "not_ready", context.run_id, "running");
        return { summary: `服务 ${manifest.instance_id} 在 ${input.timeout_seconds}s 内未 Ready。`, provenance_type: "measured", data: { instance_id: manifest.instance_id, ready: false, timeout_seconds: input.timeout_seconds, last_error: lastError }, warnings: ["服务可能仍在加载模型；请读取日志或重新探测。"], provider: { name: "openai-compatible-http" } };
      },
    }),
    createBittuneTool({
      name: "read_vllm_service_logs", label: "读取 vLLM 服务日志", recorder, parameters: Type.Object({ instance_id: Id, tail_lines: Type.Optional(Type.Integer({ minimum: 1, maximum: 1000 })) }),
      description: "读取 Bittune Service Manifest 标识服务的有限 Docker 日志。只读，完整日志保存为 Artifact。",
      async execute(params, context) {
        const input = params as { instance_id: string; tail_lines?: number }; const manifest = await context.store.getService(input.instance_id); const tailLines = input.tail_lines ?? 200; const state = await dockerServiceState(manifest, context.signal);
        const logs = await runCommand("docker", ["container", "logs", "--timestamps", "--tail", String(tailLines), manifest.container_id], { signal: context.signal, timeoutMs: 15_000, maxBytes: 1024 * 1024 }); const raw = `${logs.stdout}${logs.stderr ? `\n[stderr]\n${logs.stderr}` : ""}`; const artifact = await context.artifact("docker-logs", raw);
        await context.store.recordServiceObservation(manifest.instance_id, state.status, context.run_id);
        if (logs.exit_code !== 0) throw new BittuneError("service_logs_unavailable", `Unable to read logs for ${manifest.instance_id}.`, true, { exit_code: logs.exit_code, status: state.status });
        return { summary: `已读取 ${manifest.instance_id} 的最近 ${tailLines} 行日志。`, provenance_type: "measured", data: { instance_id: manifest.instance_id, status: state.status, running: state.running, tail_lines: tailLines, log_excerpt: tailUtf8(raw, 8 * 1024), artifact_id: artifact.artifact_id }, provider: { name: "docker-cli" } };
      },
    }),
    createBittuneTool({
      name: "inspect_vllm_service", label: "检查 vLLM 服务", recorder, parameters: Type.Object({ instance_id: Id }),
      description: "读取 Bittune 服务 Manifest、Capability 配置和 Docker 当前状态。只读。",
      async execute(params, context) { const manifest = await context.store.getService((params as { instance_id: string }).instance_id); const state = await dockerServiceState(manifest, context.signal); const observed = await context.store.recordServiceObservation(manifest.instance_id, state.status, context.run_id); return { summary: `服务 ${manifest.instance_id} 当前状态为 ${state.status}。`, provenance_type: "measured", data: manifestData(observed, state), provider: { name: "docker-cli" } }; },
    }),
    createBittuneTool({
      name: "probe_vllm_endpoint", label: "探测 vLLM 端点", recorder, parameters: Type.Object({ instance_id: Id, timeout_seconds: Type.Optional(Type.Integer({ minimum: 1 })) }),
      description: "向指定受管端点发送一次最小推理请求，判断目标模型是否可用。只读但消耗少量 GPU，不启动或修改服务。",
      async execute(params, context) {
        const input = params as { instance_id: string; timeout_seconds?: number }; const manifest = await context.store.getService(input.instance_id); const timeout = AbortSignal.timeout((input.timeout_seconds ?? 30) * 1000); const signal = context.signal ? AbortSignal.any([context.signal, timeout]) : timeout; const started = performance.now();
        let response: Response; try { response = await fetch(endpointUrl(manifest, "/v1/chat/completions"), { method: "POST", signal, headers: { "content-type": "application/json" }, body: JSON.stringify({ model: manifest.model_id, messages: [{ role: "user", content: "Reply with only OK." }], max_tokens: 4, temperature: 0 }) }); } catch (error) { if (context.signal?.aborted) throw new BittuneError("cancelled", "Endpoint probe cancelled.", true); await context.store.recordServiceObservation(manifest.instance_id, "unreachable", context.run_id, "running"); throw new BittuneError("endpoint_unavailable", `Unable to connect to ${manifest.endpoint_url}.`, true, { cause: error instanceof Error ? error.message : String(error) }); }
        const body = await response.text(); await context.artifact("vllm-endpoint-probe", body, "application/json"); if (!response.ok) { await context.store.recordServiceObservation(manifest.instance_id, `http_${response.status}`, context.run_id, "running"); throw new BittuneError("endpoint_unavailable", `Endpoint returned HTTP ${response.status}.`, true, { response: body.slice(0, 1000) }); }
        const payload = JSON.parse(body) as { model?: string; choices?: Array<{ message?: { content?: string } }> }; await context.store.recordServiceObservation(manifest.instance_id, "ready", context.run_id, "running"); return { summary: `端点可用，最小推理耗时 ${Math.round(performance.now() - started)} ms。`, provenance_type: "measured", data: { instance_id: manifest.instance_id, endpoint_url: manifest.endpoint_url, reachable: true, response_model_id: payload.model, latency_ms: Math.round(performance.now() - started), response_excerpt: payload.choices?.[0]?.message?.content?.slice(0, 200) ?? "" }, provider: { name: "vllm-openai-http" } };
      },
    }),
    createBittuneTool({
      name: "stop_vllm_service", label: "停止 vLLM 服务", recorder, parameters: Type.Object({ instance_id: Id, experiment_id: Type.Optional(Id) }),
      description: "停止一个 Bittune 受管容器。若服务归属于实验，自动化调用必须提供相同 experiment_id；不会操作外部服务。",
      async execute(params, context) {
        const input = params as { instance_id: string; experiment_id?: string }; const manifest = await context.store.getService(input.instance_id);
        if (manifest.owner && manifest.owner.experiment_id !== input.experiment_id) throw new BittuneError("service_owner_mismatch", "This service belongs to another experiment and cannot be stopped in this scope.", false);
        const stopped = await runCommand("docker", ["container", "stop", "--time", "30", manifest.container_id], { signal: context.signal, timeoutMs: 45_000 }); await context.artifact("docker-stop", `${stopped.stdout}\n${stopped.stderr}`); if (stopped.exit_code !== 0 && !/is not running/i.test(stopped.stderr)) requireSuccess(stopped);
        const state = await dockerServiceState(manifest, context.signal); const updated = await context.store.recordServiceObservation(manifest.instance_id, state.status, context.run_id, "stopped"); return { summary: `已停止受管服务 ${manifest.instance_id}。`, provenance_type: "measured", data: manifestData(updated, state), provider: { name: "docker-cli" } };
      },
    }),
  ];
}
