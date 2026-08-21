import { Type } from "typebox";
import { BittuneError } from "../../shared/errors.ts";
import { RunRecorder } from "../../shared/run-recorder.ts";
import { createBittuneTool } from "../../shared/tool.ts";

const Id = Type.String({ pattern: "^[a-z][a-z0-9-]{0,62}$" });
const Version = Type.String({ pattern: "^v[1-9][0-9]*$" });
const RunId = Type.String({ pattern: "^run-[a-f0-9-]{36}$" });

interface DiscoveryService {
  discovery_ref?: string;
  source?: string;
  management_status?: "externally_managed" | "bittune_managed" | "unknown";
  confidence?: string;
  observed_at?: string;
  source_run_id?: string;
  endpoint_url?: string;
  instance_id?: string;
  model_ids?: string[];
  service_type?: string;
  name?: string;
}

interface DiscoveryArtifact {
  artifact_ref?: string;
  source?: string;
  confidence?: string;
  observed_at?: string;
  source_run_id?: string;
  model_id?: string;
  revision?: string;
  weight_file_count?: number;
  size_bytes?: number;
}

function numeric(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

export function createDeploymentOptionTools(recorder: RunRecorder) {
  return [
    createBittuneTool({
      name: "derive_deployment_options",
      label: "推导部署候选方案",
      recorder,
      parameters: Type.Object({
        service_discovery_run_id: Type.Optional(RunId),
        model_artifact_discovery_run_id: Type.Optional(RunId),
        environment_run_id: Type.Optional(RunId),
        deployment_preset_id: Type.Optional(Id),
        deployment_preset_version: Type.Optional(Version),
      }),
      description: "基于明确的 Discovery Run、可选环境事实和 DeploymentPreset 推导复用外部端点、低影响探测或创建隔离 Bittune 服务的候选方案。它只读取已记录证据，不启动、停止、接管、下载或发布任何对象；外部性能和容量结论一律标记 validation_required。",
      async execute(params, context) {
        const input = params as {
          service_discovery_run_id?: string;
          model_artifact_discovery_run_id?: string;
          environment_run_id?: string;
          deployment_preset_id?: string;
          deployment_preset_version?: string;
        };
        if (!input.service_discovery_run_id && !input.model_artifact_discovery_run_id && !input.deployment_preset_id) {
          throw new BittuneError("invalid_input", "至少提供一个服务 Discovery Run、模型 Artifact Discovery Run 或 DeploymentPreset。", false);
        }
        const options: Array<Record<string, unknown>> = [];
        const evidence: Array<Record<string, unknown>> = [];
        let preset: Awaited<ReturnType<typeof context.store.getPreset>> | undefined;
        if (input.deployment_preset_id) preset = await context.store.getPreset(input.deployment_preset_id, input.deployment_preset_version);

        if (input.service_discovery_run_id) {
          const source = await context.store.getRun(input.service_discovery_run_id);
          if (source.manifest.tool_name !== "list_inference_services" || !source.observation.ok) {
            throw new BittuneError("invalid_evidence", "service_discovery_run_id 必须引用成功的 list_inference_services Run。", false);
          }
          const services = ((source.observation.data as { services?: DiscoveryService[] } | undefined)?.services ?? []);
          evidence.push({ run_id: input.service_discovery_run_id, tool_name: source.manifest.tool_name, observed_at: source.observation.measured_at, count: services.length });
          for (const service of services) {
            const target = {
              discovery_ref: service.discovery_ref,
              source: service.source,
              management_status: service.management_status,
              confidence: service.confidence,
              observed_at: service.observed_at,
              source_run_id: service.source_run_id,
              service_type: service.service_type,
              name: service.name,
            };
            if (service.management_status === "bittune_managed") {
              options.push({
                action: "reuse_bittune_managed_service",
                impact: "none",
                validation_status: "validation_required",
                target,
                instance_id: service.instance_id,
                endpoint_available: Boolean(service.endpoint_url),
                risks: ["服务状态、GPU 余量和端点健康度可能已变化；执行前重新 inspect/probe。"],
              });
              continue;
            }
            options.push({
              action: "reuse_external_endpoint",
              impact: "none",
              validation_status: "validation_required",
              target,
              endpoint_available: Boolean(service.endpoint_url),
              risks: ["外部服务不属于 Bittune；不得停止、重启、修改或接管。", "未实测吞吐、延迟和资源余量。"],
            });
            if (service.endpoint_url) {
              options.push({
                action: "probe_external_endpoint",
                impact: "low",
                validation_status: "validation_required",
                target,
                endpoint_available: true,
                risks: ["仅允许使用 discovery_ref 进行有界协议探测；不改变外部服务。"],
              });
            }
          }
        }

        if (input.model_artifact_discovery_run_id) {
          const source = await context.store.getRun(input.model_artifact_discovery_run_id);
          if (source.manifest.tool_name !== "list_local_model_artifacts" || !source.observation.ok) {
            throw new BittuneError("invalid_evidence", "model_artifact_discovery_run_id 必须引用成功的 list_local_model_artifacts Run。", false);
          }
          const artifacts = ((source.observation.data as { artifacts?: DiscoveryArtifact[] } | undefined)?.artifacts ?? []);
          evidence.push({ run_id: input.model_artifact_discovery_run_id, tool_name: source.manifest.tool_name, observed_at: source.observation.measured_at, count: artifacts.length });
          for (const artifact of artifacts) {
            const matchesPreset = Boolean(preset && artifact.model_id === preset.model_id && artifact.revision === preset.model_revision);
            options.push({
              action: "create_isolated_bittune_service",
              impact: "creates_bittune_service",
              validation_status: matchesPreset ? "validation_required" : "not_compatible_with_selected_preset",
              artifact: {
                artifact_ref: artifact.artifact_ref,
                source: artifact.source,
                confidence: artifact.confidence,
                observed_at: artifact.observed_at,
                source_run_id: artifact.source_run_id,
                model_id: artifact.model_id,
                revision: artifact.revision,
                weight_file_count: artifact.weight_file_count,
                size_bytes: artifact.size_bytes,
              },
              deployment_preset: preset ? { preset_id: preset.preset_id, version: preset.version, model_id: preset.model_id, model_revision: preset.model_revision } : undefined,
              risks: matchesPreset
                ? ["创建隔离服务仍需检查对应镜像、模型 Snapshot、端口和 GPU 资源。"]
                : ["Artifact 模型 ID 或 Revision 与选定 Preset 不一致；不可复制外部挂载、环境变量或命令行。"],
            });
          }
        }

        if (preset) {
          options.push({
            action: "create_isolated_bittune_service",
            impact: "creates_bittune_service",
            validation_status: "validation_required",
            deployment_preset: { preset_id: preset.preset_id, version: preset.version, model_id: preset.model_id, model_revision: preset.model_revision },
            risks: ["需要分别检查镜像、Snapshot、端口和当前 GPU 余量；本 Tool 不执行这些操作。"],
          });
        }

        let resourceSummary: Record<string, unknown> | undefined;
        if (input.environment_run_id) {
          const environment = await context.store.getRun(input.environment_run_id);
          if (environment.manifest.tool_name !== "inspect_gpu" || !environment.observation.ok) {
            throw new BittuneError("invalid_evidence", "environment_run_id 必须引用成功的 inspect_gpu Run。", false);
          }
          const gpus = ((environment.observation.data as { gpus?: Array<{ name?: string; memory_total_bytes?: number; memory_free_bytes?: number; utilization_percent?: number }> } | undefined)?.gpus ?? []);
          resourceSummary = {
            source_run_id: input.environment_run_id,
            observed_at: environment.observation.measured_at,
            gpus: gpus.map((gpu) => ({
              name: gpu.name,
              memory_total_bytes: numeric(gpu.memory_total_bytes),
              memory_free_bytes: numeric(gpu.memory_free_bytes),
              utilization_percent: numeric(gpu.utilization_percent),
            })),
          };
          evidence.push({ run_id: input.environment_run_id, tool_name: environment.manifest.tool_name, observed_at: environment.observation.measured_at, count: gpus.length });
        }

        return {
          summary: "已从 " + evidence.length + " 条证据推导 " + options.length + " 个部署候选方案。",
          provenance_type: "derived",
          data: {
            evidence,
            ...(resourceSummary ? { resource_summary: resourceSummary } : {}),
            options,
            decision_boundary: {
              external_services_read_only: true,
              external_takeover_supported: false,
              performance_claims: "validation_required",
            },
          },
          warnings: options.length ? [] : ["当前证据没有可推导的服务或模型 Artifact 候选；可重新执行对应 Discovery Tool。"],
          provider: { name: "deployment-option-deriver" },
        };
      },
    }),
  ];
}
