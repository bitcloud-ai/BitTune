import { readdir, readFile, stat } from "node:fs/promises";
import { basename, join } from "node:path";
import { Type } from "typebox";
import { BittuneError } from "../../shared/errors.ts";
import { canonicalJson, sha256 } from "../../shared/identifiers.ts";
import { redactText } from "../../shared/redaction.ts";
import { runCommand } from "../../shared/process.ts";
import { RunRecorder } from "../../shared/run-recorder.ts";
import { createBittuneTool } from "../../shared/tool.ts";
import type { ServiceManifest } from "../../shared/state-store.ts";
import {
  configuredModelRoots,
  discoveryRef,
  fields,
  relativeToRoot,
  runtimeTypeFromImage,
  serviceTypeFromRuntime,
  type DiscoveredInferenceService,
  type DiscoveredRuntime,
  type LocalModelArtifact,
} from "../../shared/discovery.ts";

const DiscoveryRef = Type.String({ pattern: "^discovery-[a-f0-9]{32}$" });
const RefInput = Type.Object({ discovery_ref: DiscoveryRef });
const ProbeInput = Type.Object({ discovery_ref: DiscoveryRef, timeout_seconds: Type.Optional(Type.Integer({ minimum: 1, maximum: 30 })) });
const Empty = Type.Object({});

interface DockerContainer { ID?: string; Names?: string; Image?: string; Ports?: string; }
interface DockerImage { Id?: string; RepoDigests?: string[]; Descriptor?: { digest?: string }; }

function imageRepository(image: string): string | undefined {
  if (/^sha256:[a-f0-9]{64}$/i.test(image)) return undefined;
  const at = image.lastIndexOf("@");
  if (at > 0) return image.slice(0, at);
  const slash = image.lastIndexOf("/"); const colon = image.lastIndexOf(":");
  return colon > slash ? image.slice(0, colon) : image || undefined;
}

async function inspectDockerImageIdentity(image: string, signal?: AbortSignal): Promise<{ repository?: string; digest?: string; image_id?: string; raw: string }> {
  try {
    const result = await runCommand("docker", ["image", "inspect", image, "--format", "{{json .}}"], { signal, timeoutMs: 15_000, maxBytes: 512 * 1024 });
    const raw = `${result.stdout}\n${result.stderr}`;
    if (result.exit_code !== 0) return { raw };
    const parsed = JSON.parse(result.stdout) as DockerImage;
    const repository = imageRepository(image);
    const matchingDigest = (parsed.RepoDigests ?? []).find((item) => !repository || item.startsWith(`${repository}@`)) ?? parsed.RepoDigests?.[0];
    const at = matchingDigest?.lastIndexOf("@");
    const digest = at && at > 0 ? matchingDigest!.slice(at + 1) : parsed.Descriptor?.digest ?? (image.includes("@sha256:") ? image.slice(image.lastIndexOf("@") + 1) : undefined);
    return { ...(repository ? { repository } : {}), ...(digest ? { digest } : {}), ...(parsed.Id ? { image_id: parsed.Id } : {}), raw };
  } catch (error) {
    if (signal?.aborted) throw error;
    return { raw: error instanceof Error ? error.message : "Docker image inspect failed." };
  }
}

function lines(value: string): string[] {
  return value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

function jsonLines<T>(value: string): T[] {
  return lines(value).flatMap((line) => {
    try { return [JSON.parse(line) as T]; } catch { return []; }
  });
}

async function runtimeFromContainer(container: DockerContainer, runId: string, managedIds: Set<string>, observedAt: string, signal?: AbortSignal): Promise<{ runtime?: DiscoveredRuntime; raw?: string }> {
  const id = container.ID;
  const image = container.Image || "";
  const runtimeType = runtimeTypeFromImage(image);
  if (!id || runtimeType === "unknown") return {};
  const identity = "docker:" + id;
  const imageObservation = await inspectDockerImageIdentity(image, signal);
  return { runtime: {
    ...fields("docker", managedIds.has(id) ? "bittune_managed" : "externally_managed", "inferred", runId, identity, observedAt),
    runtime_id: discoveryRef("runtime", identity),
    runtime_type: runtimeType,
    name: (container.Names || id.slice(0, 12)).replace(/^\//, ""),
    ...(image.includes(":") ? { version: image.slice(image.lastIndexOf(":") + 1) } : {}),
    container_id: id,
    ...(imageObservation.repository ? { runtime_image_repository: imageObservation.repository } : {}),
    ...(imageObservation.digest ? { runtime_image_digest: imageObservation.digest } : {}),
    ...(imageObservation.image_id ? { runtime_image_id: imageObservation.image_id } : {}),
  }, raw: imageObservation.raw };
}

async function discoverRuntimes(runId: string, store: { listServices: () => Promise<ServiceManifest[]> }, signal?: AbortSignal): Promise<{ runtimes: DiscoveredRuntime[]; warnings: string[]; raw: string[] }> {
  const observedAt = new Date().toISOString();
  const warnings: string[] = [];
  const raw: string[] = [];
  let managedIds = new Set<string>();
  try { managedIds = new Set((await store.listServices()).map((item) => item.container_id)); }
  catch { warnings.push("Bittune Service Manifest 不可读取；外部服务仍可继续发现。"); }
  const runtimes: DiscoveredRuntime[] = [];
  try {
    const result = await runCommand("docker", ["container", "ls", "-a", "--format", "{{json .}}"], { timeoutMs: 15000, maxBytes: 2 * 1024 * 1024 });
    raw.push(result.stdout, result.stderr);
    if (result.exit_code === 0) {
      for (const container of jsonLines<DockerContainer>(result.stdout)) {
        const observed = await runtimeFromContainer(container, runId, managedIds, observedAt, signal);
        if (observed.raw) raw.push(observed.raw);
        if (observed.runtime) runtimes.push(observed.runtime);
      }
    } else warnings.push("Docker 容器列表不可用；已继续读取进程事实。");
  } catch (error) {
    warnings.push(error instanceof Error ? error.message : "Docker Provider 不可用。");
  }
  try {
    const result = await runCommand("ps", ["-eo", "pid=,comm=,args="], { timeoutMs: 10000, maxBytes: 2 * 1024 * 1024 });
    raw.push(result.stdout, result.stderr);
    if (result.exit_code === 0) {
      for (const line of lines(result.stdout)) {
        const match = /^(\d+)\s+([^\s]+)\s+(.+)$/.exec(line);
        if (!match) continue;
        const commandLine = match[3]!.toLowerCase();
        const kind = commandLine.includes("vllm") ? "vllm" : commandLine.includes("gpustack") ? "gpustack" : commandLine.includes("text-generation-inference") || commandLine.includes("/tgi") ? "tgi" : commandLine.includes("triton") ? "triton" : undefined;
        if (!kind) continue;
        const pid = Number(match[1]);
        if (runtimes.some((item) => item.process_id === pid)) continue;
        const identity = "process:" + pid + ":" + match[2];
        runtimes.push({
          ...fields(kind === "gpustack" ? "gpu_stack" : "process", "externally_managed", "inferred", runId, identity, observedAt),
          runtime_id: discoveryRef("runtime", identity), runtime_type: kind, name: basename(match[2]!), process_id: pid,
        });
      }
    } else warnings.push("进程列表不可用；已返回 Docker 发现结果。");
  } catch (error) {
    warnings.push(error instanceof Error ? error.message : "进程 Provider 不可用。");
  }
  return { runtimes, warnings, raw };
}

interface ModelFiles { count: number; size: number; files: Array<{ name: string; size: number }> }

async function modelDirectory(directory: string): Promise<ModelFiles | undefined> {
  try {
    const entries = await readdir(directory, { withFileTypes: true });
    const weights = entries.filter((entry) => entry.isFile() && /\.(safetensors|bin|pt|pth|gguf)$/i.test(entry.name));
    if (!weights.length) return undefined;
    let size = 0;
    const files: Array<{ name: string; size: number }> = [];
    for (const file of weights) { const fileSize = (await stat(join(directory, file.name))).size; size += fileSize; files.push({ name: file.name, size: fileSize }); }
    return { count: weights.length, size, files };
  } catch {
    return undefined;
  }
}

async function allowedJson(directory: string, name: string): Promise<Record<string, unknown> | undefined> {
  try {
    const target = join(directory, name); const info = await stat(target);
    if (!info.isFile() || info.size > 256 * 1024) return undefined;
    const parsed = JSON.parse(await readFile(target, "utf8")) as unknown;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as Record<string, unknown> : undefined;
  } catch { return undefined; }
}

async function snapshotMetadata(directory: string, model: ModelFiles): Promise<LocalModelArtifact["metadata"]> {
  const [config, quantization, tokenizer, generation, safetensorsIndex, binIndex] = await Promise.all([
    allowedJson(directory, "config.json"), allowedJson(directory, "quantization_config.json"), allowedJson(directory, "tokenizer_config.json"), allowedJson(directory, "generation_config.json"), allowedJson(directory, "model.safetensors.index.json"), allowedJson(directory, "pytorch_model.bin.index.json"),
  ]);
  const architectures = config?.architectures; const architecture = Array.isArray(architectures) && typeof architectures[0] === "string" ? architectures[0] : undefined;
  const declaredDtype = typeof config?.torch_dtype === "string" ? config.torch_dtype : undefined;
  const context = typeof config?.max_position_embeddings === "number" && Number.isFinite(config.max_position_embeddings) ? config.max_position_embeddings : typeof tokenizer?.model_max_length === "number" && Number.isFinite(tokenizer.model_max_length) ? tokenizer.model_max_length : undefined;
  const quantConfig = quantization ?? (config?.quantization_config && typeof config.quantization_config === "object" ? config.quantization_config as Record<string, unknown> : undefined); const quantizationName = typeof quantConfig?.quant_method === "string" ? quantConfig.quant_method : typeof quantConfig?.bits === "number" ? `${quantConfig.bits}-bit` : undefined;
  const index = safetensorsIndex ?? binIndex; const weightMap = index?.weight_map; const indexedWeightCount = weightMap && typeof weightMap === "object" ? Object.keys(weightMap as Record<string, unknown>).length : undefined;
  return { ...(architecture ? { architecture } : {}), ...(declaredDtype ? { declared_dtype: declaredDtype } : {}), ...(context ? { declared_context_tokens: context } : {}), ...(quantizationName ? { quantization: quantizationName } : {}), ...(typeof tokenizer?.tokenizer_class === "string" ? { tokenizer_class: tokenizer.tokenizer_class } : {}), ...(typeof tokenizer?.chat_template === "string" && tokenizer.chat_template.length > 0 ? { chat_template_available: true } : {}), ...(generation ? { generation_defaults_available: true } : {}), ...(indexedWeightCount !== undefined ? { indexed_weight_count: indexedWeightCount } : {}) };
}

async function scanModelRoot(root: { kind: "huggingface" | "modelscope" | "unknown"; root: string }, runId: string): Promise<LocalModelArtifact[]> {
  const artifacts: LocalModelArtifact[] = [];
  async function visit(directory: string, depth: number): Promise<void> {
    if (depth > 5 || artifacts.length >= 200) return;
    const model = await modelDirectory(directory);
    if (model) {
      const relativePath = relativeToRoot(root.root, directory);
      const segments = relativePath.split("/").filter(Boolean);
      const modelSegment = segments.find((segment) => segment.startsWith("models--"));
      const modelId = modelSegment ? modelSegment.slice(8).replace("--", "/") : segments.slice(0, -1).join("/") || basename(directory);
      const revision = /^[a-f0-9]{40}$/.test(basename(directory)) ? basename(directory) : undefined;
      const identity = root.kind + ":" + relativePath;
      const metadata = await snapshotMetadata(directory, model);
      artifacts.push({
        ...fields(root.kind === "unknown" ? "huggingface" : root.kind, "externally_managed", "exact", runId, identity),
        artifact_ref: discoveryRef("model", identity), artifact_type: "model_snapshot", model_id: modelId,
        ...(revision ? { revision } : {}), cache_kind: root.kind, weight_file_count: model.count, size_bytes: model.size,
        metadata_fingerprint: sha256(canonicalJson({ model_id: modelId, revision, files: model.files, metadata })), metadata,
      });
      return;
    }
    let entries;
    try { entries = await readdir(directory, { withFileTypes: true }); } catch { return; }
    for (const entry of entries) if (entry.isDirectory() && !entry.name.startsWith(".")) await visit(join(directory, entry.name), depth + 1);
  }
  await visit(root.root, 0);
  return artifacts;
}

function portFromDocker(value?: string): number | undefined {
  const match = /(?:^|,\s*)(?:127\.0\.0\.1|0\.0\.0\.0|localhost):([0-9]+)->/.exec(value || "");
  const port = Number(match?.[1]);
  return Number.isInteger(port) && port > 0 && port < 65536 ? port : undefined;
}

function processKind(commandLine: string): DiscoveredRuntime["runtime_type"] | undefined {
  const normalized = commandLine.toLowerCase();
  if (normalized.includes("vllm")) return "vllm";
  if (normalized.includes("gpustack")) return "gpustack";
  if (normalized.includes("text-generation-inference") || normalized.includes("/tgi")) return "tgi";
  if (normalized.includes("triton")) return "triton";
  return undefined;
}

function processPorts(value: string): Map<number, number> {
  const ports = new Map<number, number>();
  for (const line of lines(value)) {
    const pid = Number(/pid[=](\d+)/i.exec(line)?.[1]);
    const port = Number(/(?:127\.0\.0\.1|0\.0\.0\.0|\[::\]|localhost):(\d+)/.exec(line)?.[1]);
    if (Number.isInteger(pid) && pid > 0 && Number.isInteger(port) && port > 0 && port < 65536) ports.set(pid, port);
  }
  return ports;
}

function serviceFromContainer(container: DockerContainer, runId: string, managedIds: Set<string>, observedAt: string): DiscoveredInferenceService | undefined {
  const id = container.ID;
  const runtimeType = runtimeTypeFromImage(container.Image || "");
  if (!id || runtimeType === "unknown") return undefined;
  const identity = "docker:" + id;
  const port = portFromDocker(container.Ports);
  return {
    ...fields("docker", managedIds.has(id) ? "bittune_managed" : "externally_managed", "inferred", runId, identity, observedAt),
    service_ref: discoveryRef("service", identity), service_type: serviceTypeFromRuntime(runtimeType),
    name: (container.Names || id.slice(0, 12)).replace(/^\//, ""),
    ...(port ? { endpoint_url: "http://127.0.0.1:" + port } : {}), container_id: id,
  };
}

async function discoverServices(runId: string, store: { listServices: () => Promise<ServiceManifest[]> }): Promise<{ services: DiscoveredInferenceService[]; warnings: string[]; raw: string[] }> {
  const observedAt = new Date().toISOString();
  const warnings: string[] = [];
  const raw: string[] = [];
  let manifests: ServiceManifest[] = [];
  try { manifests = await store.listServices(); }
  catch { warnings.push("Bittune Service Manifest 不可读取；已继续发现外部服务。"); }
  const managedIds = new Set(manifests.map((item) => item.container_id));
  const services: DiscoveredInferenceService[] = manifests.map((manifest) => {
    const identity = "managed:" + manifest.instance_id;
    return {
      ...fields("docker", "bittune_managed", "exact", runId, identity, observedAt),
      service_ref: discoveryRef("service", identity), service_type: "vllm", name: manifest.service_name,
      endpoint_url: manifest.endpoint_url, model_ids: [manifest.model_id], container_id: manifest.container_id, instance_id: manifest.instance_id,
    };
  });
  try {
    const result = await runCommand("docker", ["container", "ls", "-a", "--format", "{{json .}}"], { timeoutMs: 15000, maxBytes: 2 * 1024 * 1024 });
    raw.push(result.stdout, result.stderr);
    if (result.exit_code === 0) {
      for (const container of jsonLines<DockerContainer>(result.stdout)) {
        const service = serviceFromContainer(container, runId, managedIds, observedAt);
        if (service && !services.some((item) => item.container_id === service.container_id)) services.push(service);
      }
    } else warnings.push("Docker 服务列表不可用；已返回 Bittune Manifest 中的服务。");
  } catch (error) {
    warnings.push(error instanceof Error ? error.message : "Docker Provider 不可用。");
  }
  try {
    const processResult = await runCommand("ps", ["-eo", "pid=,comm=,args="], { timeoutMs: 10000, maxBytes: 2 * 1024 * 1024 });
    let portResult: { exit_code: number | null; stdout: string; stderr: string };
    try {
      portResult = await runCommand("ss", ["-ltnp"], { timeoutMs: 10000, maxBytes: 1024 * 1024 });
    } catch {
      portResult = { exit_code: 1, stdout: "", stderr: "ss unavailable" };
    }
    raw.push(processResult.stdout, processResult.stderr, portResult.stdout, portResult.stderr);
    if (processResult.exit_code === 0) {
      const ports = processPorts(portResult.exit_code === 0 ? portResult.stdout : "");
      for (const line of lines(processResult.stdout)) {
        const match = /^(\d+)\s+([^\s]+)\s+(.+)$/.exec(line);
        if (!match) continue;
        const kind = processKind(match[3]!);
        const processId = Number(match[1]);
        if (!kind || !Number.isInteger(processId) || services.some((item) => item.process_id === processId)) continue;
        const identity = "process:" + processId + ":" + match[2];
        const port = ports.get(processId);
        services.push({
          ...fields(kind === "gpustack" ? "gpu_stack" : "process", "externally_managed", "inferred", runId, identity, observedAt),
          service_ref: discoveryRef("service", identity), service_type: serviceTypeFromRuntime(kind), name: basename(match[2]!),
          ...(port ? { endpoint_url: "http://127.0.0.1:" + port } : {}), process_id: processId,
        });
      }
    } else warnings.push("进程服务列表不可用；已返回 Docker 服务事实。");
  } catch (error) {
    warnings.push(error instanceof Error ? error.message : "进程服务 Provider 不可用。");
  }
  return { services, warnings, raw };
}

function findRef<T extends { discovery_ref: string }>(items: T[], ref: string): T {
  const item = items.find((candidate) => candidate.discovery_ref === ref);
  if (!item) throw new BittuneError("discovery_not_found", "找不到本次主机发现中的对象 " + ref + "。", false);
  return item;
}

function allowedEndpoint(endpoint: string): URL {
  let url: URL;
  try { url = new URL(endpoint); } catch { throw new BittuneError("endpoint_not_allowed", "发现的端点地址无效。", false); }
  if (url.protocol !== "http:" || url.username || url.password) throw new BittuneError("endpoint_not_allowed", "只允许无凭据的本机 HTTP 端点。", false);
  const host = url.hostname.toLowerCase();
  const privateHost = host === "localhost" || host === "::1" || /^127\./.test(host) || /^10\./.test(host) || /^192\.168\./.test(host) || /^172\.(1[6-9]|2\d|3[0-1])\./.test(host);
  if (!privateHost) throw new BittuneError("endpoint_not_allowed", "端点不在本机或允许的私网范围内。", false);
  return url;
}

export function createDiscoveryTools(recorder: RunRecorder) {
  return [
    createBittuneTool({
      name: "list_inference_runtimes", label: "发现推理 Runtime", parameters: Empty, recorder,
      description: "只读发现当前宿主上已存在的 vLLM、GPUStack、TGI 或 Triton 推理 Runtime。使用固定 Docker/进程读取，不启动、停止、修改或下载任何对象；Docker Runtime 在可用时附带镜像 Repository、Digest 和 Image ID，结果带来源、归属、置信度、采集时间和短期 discovery_ref。",
      async execute(_params, context) {
        const result = await discoverRuntimes(context.run_id, context.store, context.signal);
        if (result.raw.some(Boolean)) await context.artifact("inference-runtime-discovery", redactText(result.raw.join("\n")));
        return { summary: "发现 " + result.runtimes.length + " 个推理 Runtime。", provenance_type: "measured", data: { runtimes: result.runtimes }, warnings: result.warnings, provider: { name: "host-runtime-discovery" } };
      },
    }),
    createBittuneTool({
      name: "inspect_inference_runtime", label: "检查推理 Runtime", parameters: RefInput, recorder,
      description: "检查 list_inference_runtimes 返回的短期 discovery_ref 对应的 Runtime，并重新读取 Docker 镜像身份（如果可用）。只读重新采集事实；不接受任意容器 ID、进程 ID、命令或路径。",
      async execute(params, context) {
        const result = await discoverRuntimes(context.run_id, context.store, context.signal);
        const runtime = findRef(result.runtimes, (params as { discovery_ref: string }).discovery_ref);
        return { summary: "读取 " + runtime.name + "（" + runtime.runtime_type + "）Runtime 发现事实。", provenance_type: "measured", data: { runtime }, warnings: result.warnings, provider: { name: "host-runtime-discovery" } };
      },
    }),
    createBittuneTool({
      name: "list_local_model_artifacts", label: "发现本地模型 Artifact", parameters: Empty, recorder,
      description: "只读扫描管理员配置的 Hugging Face/ModelScope 缓存根，发现本地权重 Snapshot，并仅解析 allowlist 中的 config、量化、tokenizer、generation 与权重索引 JSON。不会加载权重、执行模型代码、读取任意路径或下载模型。",
      async execute(_params, context) {
        const roots = configuredModelRoots();
        const artifacts = (await Promise.all(roots.map((root) => scanModelRoot(root, context.run_id)))).flat();
        const warnings: string[] = [];
        for (const root of roots) {
          try { if (!(await stat(root.root)).isDirectory()) warnings.push("模型缓存根目录不可用：" + root.kind + "。"); }
          catch { warnings.push("未发现可读取的 " + root.kind + " 模型缓存根目录。"); }
        }
        return { summary: "发现 " + artifacts.length + " 个本地模型 Artifact。", provenance_type: "measured", data: { artifacts, scanned_sources: roots.map((root) => ({ source: root.kind })) }, warnings, provider: { name: "model-cache-discovery" } };
      },
    }),
    createBittuneTool({
      name: "list_inference_services", label: "发现推理服务", parameters: Empty, recorder,
      description: "只读发现当前宿主上已存在的 Bittune 受管或外部推理服务，并标记 source、management_status、confidence、observed_at、source_run_id 和 discovery_ref。不会启动、停止、接管或修改服务。",
      async execute(_params, context) {
        const result = await discoverServices(context.run_id, context.store);
        if (result.raw.some(Boolean)) await context.artifact("inference-service-discovery", redactText(result.raw.join("\n")));
        return { summary: "发现 " + result.services.length + " 个推理服务。", provenance_type: "measured", data: { services: result.services }, warnings: result.warnings, provider: { name: "host-service-discovery" } };
      },
    }),
    createBittuneTool({
      name: "inspect_inference_service", label: "检查推理服务", parameters: RefInput, recorder,
      description: "检查 list_inference_services 返回的短期 discovery_ref 对应的服务。只读重新采集事实；不接受任意 URL、容器 ID 或服务配置。",
      async execute(params, context) {
        const result = await discoverServices(context.run_id, context.store);
        const service = findRef(result.services, (params as { discovery_ref: string }).discovery_ref);
        return { summary: "读取 " + service.name + " 推理服务发现事实。", provenance_type: "measured", data: { service }, warnings: result.warnings, provider: { name: "host-service-discovery" } };
      },
    }),
    createBittuneTool({
      name: "probe_inference_endpoint", label: "探测发现的推理端点", parameters: ProbeInput, recorder,
      description: "对 list_inference_services 返回的本机或允许私网 discovery_ref 执行一次有边界的 OpenAI-compatible /v1/models 探测。只读、限制超时和响应大小，不接受任意 URL，不启动或修改服务。",
      async execute(params, context) {
        const input = params as { discovery_ref: string; timeout_seconds?: number };
        const result = await discoverServices(context.run_id, context.store);
        const service = findRef(result.services, input.discovery_ref);
        if (!service.endpoint_url) throw new BittuneError("endpoint_unavailable", "发现的服务没有可探测的本机端点。", false);
        const endpoint = allowedEndpoint(service.endpoint_url);
        const timeout = AbortSignal.timeout((input.timeout_seconds || 10) * 1000);
        const signal = context.signal ? AbortSignal.any([context.signal, timeout]) : timeout;
        const started = performance.now();
        let response: Response;
        try { response = await fetch(new URL("/v1/models", endpoint), { signal, headers: { accept: "application/json" } }); }
        catch (error) {
          if (context.signal?.aborted) throw new BittuneError("cancelled", "推理端点探测已取消。", true);
          throw new BittuneError("endpoint_unavailable", "无法连接发现的端点。", true, { cause: error instanceof Error ? redactText(error.message) : "unknown" });
        }
        const body = (await response.text()).slice(0, 64 * 1024);
        await context.artifact("inference-endpoint-probe", body, "application/json");
        let payload: { data?: Array<{ id?: string }> } = {};
        try { payload = JSON.parse(body) as typeof payload; } catch { /* bounded raw response */ }
        if (!response.ok) throw new BittuneError("endpoint_unavailable", "发现的端点返回 HTTP " + response.status + "。", true, { response: redactText(body).slice(0, 1000) });
        return { summary: "发现的端点 " + service.endpoint_url + " 可达。", provenance_type: "measured", data: { discovery_ref: service.discovery_ref, endpoint_url: service.endpoint_url, reachable: true, latency_ms: Math.round(performance.now() - started), model_ids: payload.data?.map((item) => item.id).filter((id): id is string => Boolean(id)) || [] }, warnings: result.warnings, provider: { name: "openai-compatible-http" } };
      },
    }),
  ];
}
