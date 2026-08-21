import { Type } from "typebox";
import { RunRecorder } from "../../shared/run-recorder.ts";
import { createBittuneTool } from "../../shared/tool.ts";
import { VLLM_SERVING_PARAMETERS, type ServingConfiguration } from "../../shared/vllm-capabilities.ts";

const Id = Type.String({ pattern: "^[a-z][a-z0-9-]{0,62}$" });
const Version = Type.String({ pattern: "^v[1-9][0-9]*$" });
const Revision = Type.String({ pattern: "^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$" });
const Digest = Type.String({ pattern: "^sha256:[a-f0-9]{64}$" });
const ModelId = Type.String({ pattern: "^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)?$", maxLength: 256 });
const Repository = Type.String({ pattern: "^[a-z0-9][a-z0-9._:/-]{0,255}$", maxLength: 256 });
const ServingConfiguration = Type.Partial(Type.Object({
  dtype: Type.Union([Type.Literal("auto"), Type.Literal("bfloat16"), Type.Literal("float16"), Type.Literal("float32")]),
  tensor_parallel_size: Type.Integer({ minimum: 1 }), pipeline_parallel_size: Type.Integer({ minimum: 1 }),
  gpu_device_ids: Type.Array(Type.Integer({ minimum: 0 })), max_model_len: Type.Integer({ minimum: 1 }),
  max_num_seqs: Type.Integer({ minimum: 1 }), max_num_batched_tokens: Type.Integer({ minimum: 1 }),
  gpu_memory_utilization: Type.Number({ exclusiveMinimum: 0, maximum: 1 }), swap_space_gib: Type.Number({ minimum: 0 }),
  enforce_eager: Type.Boolean(), enable_prefix_caching: Type.Boolean(),
}));
const RunId = Type.String({ pattern: "^run-[a-f0-9-]{36}$" });

export function createDeploymentPresetTools(recorder: RunRecorder) {
  return [
    createBittuneTool({
      name: "list_deployment_presets", label: "列出部署定义", recorder, parameters: Type.Object({}),
      description: "列出 Bittune 已保存的 DeploymentPreset 不可变版本。只读取状态，不探测模型、镜像或宿主；没有内置模型种子。",
      async execute(_params, context) {
        const presets = await context.store.listPresets();
        return { summary: `发现 ${presets.length} 个 DeploymentPreset 版本。`, provenance_type: "stored", data: { presets }, provider: { name: "file-run-store" } };
      },
    }),
    createBittuneTool({
      name: "get_deployment_preset", label: "读取部署定义", recorder, parameters: Type.Object({ preset_id: Id, version: Type.Optional(Version) }),
      description: "读取指定 DeploymentPreset 的模型身份、Runtime 身份和有限 serving_configuration。只读，不检查本机资产。",
      async execute(params, context) {
        const input = params as { preset_id: string; version?: string };
        const preset = await context.store.getPreset(input.preset_id, input.version);
        return { summary: `读取 DeploymentPreset ${preset.preset_id}/${preset.version}。`, provenance_type: "stored", data: preset, provider: { name: "file-run-store" } };
      },
    }),
    createBittuneTool({
      name: "publish_deployment_preset", label: "发布部署定义", recorder,
      parameters: Type.Object({
        preset_id: Id, expected_parent_version: Type.Optional(Version), model_id: ModelId, model_revision: Revision,
        artifact_fingerprint: Type.Optional(Digest), runtime_kind: Type.Literal("vllm"), runtime_image_repository: Repository,
        runtime_image_digest: Digest, serving_configuration: Type.Optional(ServingConfiguration), source_run_ids: Type.Optional(Type.Array(RunId, { maxItems: 32 })),
      }),
      description: "追加一个通用 DeploymentPreset 版本。调用方选择模型、Revision、Runtime 镜像和 Capability 范围内的配置；只保存定义，不下载、部署、探测或声称已验证。",
      async execute(params, context) {
        const input = params as { preset_id: string; expected_parent_version?: string; model_id: string; model_revision: string; artifact_fingerprint?: string; runtime_kind: "vllm"; runtime_image_repository: string; runtime_image_digest: string; serving_configuration?: ServingConfiguration; source_run_ids?: string[] };
        const preset = await context.store.publishPreset({
          preset_id: input.preset_id, ...(input.expected_parent_version === undefined ? {} : { expected_parent_version: input.expected_parent_version }),
          model_id: input.model_id, model_revision: input.model_revision, ...(input.artifact_fingerprint ? { artifact_fingerprint: input.artifact_fingerprint } : {}),
          runtime_kind: input.runtime_kind, runtime_image_repository: input.runtime_image_repository, runtime_image_digest: input.runtime_image_digest,
          serving_configuration: input.serving_configuration ?? {}, source_run_ids: input.source_run_ids ?? [],
        });
        return { summary: `已发布 DeploymentPreset ${preset.preset_id}/${preset.version}。`, provenance_type: "stored", data: preset, provider: { name: "file-run-store", version: VLLM_SERVING_PARAMETERS.length.toString() } };
      },
    }),
  ];
}
