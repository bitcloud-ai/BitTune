import { Type } from "typebox";
import { BittuneError } from "../../shared/errors.ts";
import { canonicalJson, requireSha256, sha256 } from "../../shared/identifiers.ts";
import { RunRecorder } from "../../shared/run-recorder.ts";
import type { CapacityBaseline, MeasuredOperatingPoint, RunManifest } from "../../shared/state-store.ts";
import { createBittuneTool } from "../../shared/tool.ts";
import { materializeVllmConfiguration, vllmConfigurationFingerprint } from "../../shared/vllm-capabilities.ts";

const Id = Type.String({ pattern: "^[a-z][a-z0-9-]{0,62}$" });
const Version = Type.String({ pattern: "^v[1-9][0-9]*$" });
const RunId = Type.String({ pattern: "^run-[a-f0-9-]{36}$" });
const Digest = Type.String({ pattern: "^sha256:[a-f0-9]{64}$" });
interface CandidateBaseline extends Omit<CapacityBaseline, "schema_version" | "version" | "created_at" | "content_hash"> { candidate_artifact_id: string }

function serviceFromRun(run: { observation: { data?: unknown } }): Record<string, unknown> {
  const service = (run.observation.data as { service?: unknown } | undefined)?.service;
  if (!service || typeof service !== "object") throw new BittuneError("evidence_incomplete", "Service Run lacks a managed service manifest.", false);
  return service as Record<string, unknown>;
}
function environmentFingerprint(data: unknown): string {
  const value = data as { host_id?: string; gpus?: Array<Record<string, unknown>> } | undefined;
  if (!value?.gpus?.length) throw new BittuneError("evidence_incomplete", "Environment Run lacks GPU observations.", false);
  return sha256(canonicalJson({ host_id: value.host_id, gpus: value.gpus.map((gpu) => ({ uuid: gpu.uuid, name: gpu.name, driver_version: gpu.driver_version, compute_capability: gpu.compute_capability, memory_total_bytes: gpu.memory_total_bytes })) }));
}
function runDuration(manifest: RunManifest): number { const start = Date.parse(manifest.started_at); const finish = Date.parse(manifest.finished_at); return Number.isFinite(start) && Number.isFinite(finish) ? Math.max(0, (finish - start) / 1000) : 0; }
type CapacityEvidenceKind = "environment" | "service" | "probe" | "benchmark";
function capacityEvidenceKind(toolName: string): CapacityEvidenceKind | undefined {
  if (toolName === "inspect_gpu") return "environment";
  if (toolName === "start_vllm_service" || toolName === "inspect_vllm_service") return "service";
  if (toolName === "probe_vllm_endpoint") return "probe";
  if (toolName === "run_performance_test") return "benchmark";
  return undefined;
}

export function createCapacityBaselineTools(recorder: RunRecorder) {
  return [
    createBittuneTool({
      name: "list_capacity_baselines", label: "列出容量基线", recorder, parameters: Type.Object({ deployment_preset_id: Type.Optional(Id) }),
      description: "列出已经发布的、由实际 Run Record 支持的 MeasuredOperatingPoint。只读，不估算、不探测、不压测。",
      async execute(params, context) { const input = params as { deployment_preset_id?: string }; const baselines = (await context.store.listBaselines()).filter((item) => !input.deployment_preset_id || item.deployment_preset_id === input.deployment_preset_id); return { summary: `发现 ${baselines.length} 个 CapacityBaseline 版本。`, provenance_type: "stored", data: { baselines }, provider: { name: "file-run-store" } }; },
    }),
    createBittuneTool({
      name: "get_capacity_baseline", label: "读取容量基线", recorder, parameters: Type.Object({ baseline_id: Id, version: Type.Optional(Version) }),
      description: "读取指定 CapacityBaseline 版本及其运行点、证据 Run ID 和验证状态。只读。",
      async execute(params, context) { const input = params as { baseline_id: string; version?: string }; const baseline = await context.store.getBaseline(input.baseline_id, input.version); return { summary: `读取 CapacityBaseline ${baseline.baseline_id}/${baseline.version}。`, provenance_type: "stored", data: baseline, provider: { name: "file-run-store" } }; },
    }),
    createBittuneTool({
      name: "derive_capacity_baseline", label: "推导容量运行点", recorder,
      parameters: Type.Object({ deployment_preset_id: Id, source_run_ids: Type.Array(RunId, { minItems: 4, maxItems: 32 }) }),
      description: "读取调用方明确指定的环境、受管服务、端点和性能 Run，校验模型/Runtime/配置/负载证据一致性，生成一个可复核的 MeasuredOperatingPoint 候选。单次成功只形成运行点，不生成最大容量结论；不运行新探测、部署或压测。",
      async execute(params, context) {
        const input = params as { deployment_preset_id: string; source_run_ids: string[] }; const uniqueIds = [...new Set(input.source_run_ids)]; if (uniqueIds.length !== input.source_run_ids.length) throw new BittuneError("invalid_input", "source_run_ids must be unique.", false);
        const sources = await Promise.all(uniqueIds.map((runId) => context.store.getRun(runId))); if (sources.some(({ manifest }) => !manifest || manifest.status !== "completed")) throw new BittuneError("evidence_incomplete", "All source Run Records must be completed.", false);
        const evidenceCounts = new Map<CapacityEvidenceKind, number>();
        for (const source of sources) {
          const kind = capacityEvidenceKind(source.manifest.tool_name);
          if (!kind) throw new BittuneError("evidence_conflict", `Run ${source.manifest.run_id} (${source.manifest.tool_name}) is not valid CapacityBaseline evidence.`, false);
          evidenceCounts.set(kind, (evidenceCounts.get(kind) ?? 0) + 1);
        }
        const requiredKinds: CapacityEvidenceKind[] = ["environment", "service", "probe", "benchmark"];
        if (requiredKinds.some((kind) => evidenceCounts.get(kind) !== 1)) throw new BittuneError("evidence_conflict", "CapacityBaseline requires exactly one completed Run for inspect_gpu, managed service, probe_vllm_endpoint and run_performance_test.", false);
        const environment = sources.find((source) => capacityEvidenceKind(source.manifest.tool_name) === "environment"); const serviceRun = sources.find((source) => capacityEvidenceKind(source.manifest.tool_name) === "service"); const probe = sources.find((source) => capacityEvidenceKind(source.manifest.tool_name) === "probe"); const benchmark = sources.find((source) => capacityEvidenceKind(source.manifest.tool_name) === "benchmark");
        if (!environment || !serviceRun || !probe || !benchmark) throw new BittuneError("evidence_incomplete", "Sources must include inspect_gpu, a managed service observation, probe_vllm_endpoint and run_performance_test.", false);
        const service = serviceFromRun(serviceRun); const presetVersion = String(service.deployment_preset_version ?? ""); const preset = await context.store.getPreset(input.deployment_preset_id, presetVersion);
        if (service.deployment_preset_id !== preset.preset_id || service.model_id !== preset.model_id || service.model_revision !== preset.model_revision || service.runtime_kind !== preset.runtime_kind || service.runtime_image_repository !== preset.runtime_image_repository || service.runtime_image_digest !== preset.runtime_image_digest) throw new BittuneError("evidence_conflict", "Service identity does not match the selected DeploymentPreset.", false);
        const probeData = probe.observation.data as { instance_id?: string; response_model_id?: string } | undefined; const benchmarkData = benchmark.observation.data as { service_instance_id?: string; metrics?: Record<string, unknown>; requested?: Record<string, unknown>; observed_gpu_memory_peak_bytes?: number } | undefined;
        if (!probeData || probeData.instance_id !== service.instance_id || (probeData.response_model_id && probeData.response_model_id !== preset.model_id) || benchmarkData?.service_instance_id !== service.instance_id) throw new BittuneError("evidence_conflict", "Probe and benchmark do not refer to the same managed service.", false);
        if (!benchmarkData?.metrics || !Object.keys(benchmarkData.metrics).length) throw new BittuneError("evidence_incomplete", "Benchmark Run lacks normalized metrics.", false);
        const environmentFp = environmentFingerprint(environment.observation.data); const configuration = materializeVllmConfiguration(service.serving_configuration ?? {}); const configFp = vllmConfigurationFingerprint(configuration); const workloadFp = sha256(canonicalJson(benchmarkData.requested ?? (benchmark.manifest.input as Record<string, unknown> ?? {})));
        const deploymentFp = sha256(canonicalJson({ preset_id: preset.preset_id, preset_version: preset.version, model_id: preset.model_id, model_revision: preset.model_revision, runtime_kind: preset.runtime_kind, runtime_image_digest: preset.runtime_image_digest }));
        const successRate = typeof benchmarkData.metrics.success_rate === "number" ? benchmarkData.metrics.success_rate : undefined; const status: MeasuredOperatingPoint["verification_status"] = successRate !== undefined && successRate >= 0.95 ? "verified" : "validation_required";
        const operatingPoint: MeasuredOperatingPoint = { deployment_fingerprint: deploymentFp, environment_fingerprint: environmentFp, workload_fingerprint: workloadFp, serving_configuration: configuration, configuration_fingerprint: configFp, metrics: benchmarkData.metrics, gates: { success_rate: successRate ?? "missing" }, source_run_ids: uniqueIds, measured_at: benchmark.observation.measured_at, verification_status: status };
        const base = { baseline_id: `${preset.preset_id}-${configFp.slice(7, 19)}`, deployment_preset_id: preset.preset_id, deployment_preset_version: preset.version, operating_points: [operatingPoint], source_run_ids: uniqueIds, candidate_run_id: context.run_id };
        const candidateHash = sha256(canonicalJson(base)); const artifact = await context.artifact("capacity-baseline-candidate", JSON.stringify({ ...base, candidate_hash: candidateHash }, null, 2), "application/json"); const candidate: CandidateBaseline = { ...base, candidate_hash: candidateHash, candidate_artifact_id: artifact.artifact_id };
        return { summary: `已生成一个 ${status} CapacityOperatingPoint 候选；它不代表最大容量。`, provenance_type: "derived", data: candidate, provider: { name: "capacity-baseline-deriver" } };
      },
    }),
    createBittuneTool({
      name: "publish_capacity_baseline", label: "发布容量基线", recorder, parameters: Type.Object({ candidate_run_id: RunId, candidate_hash: Digest }),
      description: "校验 derive_capacity_baseline 生成的候选 Hash、Preset 与源 Run 后追加一个不可变 CapacityBaseline。证据缺失、Hash 不匹配或非 verified operating point 时不发布为已验证结论。",
      async execute(params, context) {
        const input = params as { candidate_run_id: string; candidate_hash: string }; requireSha256(input.candidate_hash, "candidate_hash"); const candidateRun = await context.store.getRun(input.candidate_run_id);
        if (candidateRun.manifest.tool_name !== "derive_capacity_baseline" || !candidateRun.observation.ok) throw new BittuneError("invalid_candidate", "candidate_run_id must refer to a successful derive run.", false);
        const artifact = candidateRun.manifest.artifacts.find((item) => item.label === "capacity-baseline-candidate"); if (!artifact) throw new BittuneError("invalid_candidate", "Candidate artifact is missing.", false); const raw = await context.store.readArtifact(input.candidate_run_id, artifact.artifact_id, 0, 65_536); if (raw.truncated) throw new BittuneError("invalid_candidate", "Candidate artifact exceeds the bounded reader limit.", false);
        const candidate = JSON.parse(raw.text) as CandidateBaseline; if (candidate.candidate_hash !== input.candidate_hash) throw new BittuneError("candidate_hash_mismatch", "candidate_hash does not match candidate content.", false); if (!Array.isArray(candidate.operating_points) || candidate.operating_points.length === 0 || candidate.operating_points.some((point) => point.verification_status !== "verified")) throw new BittuneError("evidence_incomplete", "Only verified operating points can be published as a CapacityBaseline.", false); const { candidate_hash: _candidateHash, candidate_artifact_id: _artifactId, ...withoutHash } = candidate; if (sha256(canonicalJson(withoutHash)) !== input.candidate_hash) throw new BittuneError("candidate_hash_mismatch", "Candidate content hash verification failed.", false);
        await context.store.getPreset(candidate.deployment_preset_id, candidate.deployment_preset_version); const published = await context.store.publishBaseline({ ...withoutHash, candidate_hash: input.candidate_hash }); return { summary: `已发布 CapacityBaseline ${published.baseline_id}/${published.version}。`, provenance_type: "stored", data: published, provider: { name: "file-run-store" } };
      },
    }),
  ];
}
