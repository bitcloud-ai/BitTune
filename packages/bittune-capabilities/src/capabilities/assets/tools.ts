import { Type } from "typebox";
import { BittuneError } from "../../shared/errors.ts";
import { requireSuccess, runCommand } from "../../shared/process.ts";
import { RunRecorder } from "../../shared/run-recorder.ts";
import { createBittuneTool } from "../../shared/tool.ts";
import { resolveVllmImage } from "../../shared/vllm-image.ts";

const Id = Type.String({ pattern: "^[a-z][a-z0-9-]{0,62}$" });
const Version = Type.String({ pattern: "^v[1-9][0-9]*$" });

async function presetFor(params: unknown, context: { store: { getPreset: (id: string, version?: string) => Promise<any> } }) {
  const input = params as { deployment_preset_id: string; deployment_preset_version?: string };
  return context.store.getPreset(input.deployment_preset_id, input.deployment_preset_version);
}

export function createAssetTools(recorder: RunRecorder) {
  const presetParameters = Type.Object({ deployment_preset_id: Id, deployment_preset_version: Type.Optional(Version) });
  return [
    createBittuneTool({
      name: "inspect_vllm_image", label: "检查 vLLM 镜像", recorder, parameters: presetParameters,
      description: "检查指定 DeploymentPreset 固定引用的 vLLM 镜像 Digest 是否已经存在于本机 Docker。仅读取镜像状态，不拉取镜像、不启动容器。",
      async execute(params, context) {
        const preset = await presetFor(params, context);
        const reference = `${preset.runtime_image_repository}@${preset.runtime_image_digest}`;
        const resolution = await resolveVllmImage({ repository: preset.runtime_image_repository, digest: preset.runtime_image_digest }, context.signal);
        await context.artifact("docker-image-inspect", resolution.diagnostics);
        if (!resolution.image) return { summary: `本机不存在 ${reference}。`, provenance_type: "measured", data: { deployment_preset_id: preset.preset_id, deployment_preset_version: preset.version, image_reference: reference, available: false }, warnings: ["可在用户目标需要该镜像时调用 pull_vllm_image。"], provider: { name: "docker-cli" } };
        return { summary: `本机已存在 vLLM 镜像 ${reference}。`, provenance_type: "measured", data: { deployment_preset_id: preset.preset_id, deployment_preset_version: preset.version, available: true, ...resolution.image }, ...(resolution.image.source === "offline_image_id" ? { warnings: ["镜像由离线包导入；Docker 未恢复 RepoDigest，已按 OCI Descriptor 校验后使用本地 Image ID。"] } : {}), provider: { name: "docker-cli" } };
      },
    }),
    createBittuneTool({
      name: "pull_vllm_image", label: "拉取 vLLM 镜像", recorder, parameters: presetParameters,
      description: "拉取指定 DeploymentPreset 引用的 Runtime 镜像 Digest。该操作使用网络和磁盘；不下载模型、不探测环境、不启动或探测服务。",
      async execute(params, context) {
        const preset = await presetFor(params, context);
        const reference = `${preset.runtime_image_repository}@${preset.runtime_image_digest}`;
        const existing = await resolveVllmImage({ repository: preset.runtime_image_repository, digest: preset.runtime_image_digest }, context.signal);
        if (existing.image) return { summary: `本机已存在 vLLM 镜像 ${reference}，无需拉取。`, provenance_type: "measured", data: existing.image, provider: { name: "docker-cli" } };
        context.update(`正在拉取 Runtime 镜像 ${reference}…`);
        const pull = requireSuccess(await runCommand("docker", ["pull", reference], { signal: context.signal, timeoutMs: 30 * 60_000, maxBytes: 2 * 1024 * 1024 }));
        await context.artifact("docker-image-pull", `${pull.stdout}\n${pull.stderr}`);
        const resolved = await resolveVllmImage({ repository: preset.runtime_image_repository, digest: preset.runtime_image_digest }, context.signal);
        if (!resolved.image) throw new BittuneError("image_missing", `镜像拉取后仍无法验证 ${reference}。`, true);
        return { summary: `已拉取 vLLM 镜像 ${reference}。`, provenance_type: "measured", data: resolved.image, provider: { name: "docker-cli" } };
      },
    }),
    createBittuneTool({
      name: "inspect_model_snapshot", label: "检查模型快照", recorder, parameters: presetParameters,
      description: "检查指定 DeploymentPreset 固定模型 Revision 的本地 Hugging Face Snapshot 是否完整存在。只读缓存，不访问网络、不下载模型。",
      async execute(params, context) {
        const preset = await presetFor(params, context);
        const snapshotPath = await context.store.modelSnapshotPath(preset);
        const available = await context.store.snapshotExists(preset);
        return { summary: available ? `本机存在模型 Snapshot ${preset.model_id}@${preset.model_revision}。` : `本机不存在模型 Snapshot ${preset.model_id}@${preset.model_revision}。`, provenance_type: "measured", data: { deployment_preset_id: preset.preset_id, deployment_preset_version: preset.version, model_id: preset.model_id, model_revision: preset.model_revision, available, snapshot_identity: `${preset.model_id}@${preset.model_revision}` }, warnings: available ? [] : ["可在用户目标需要该模型时调用 download_model_snapshot。"], provider: { name: "huggingface-cache" } };
      },
    }),
    createBittuneTool({
      name: "download_model_snapshot", label: "下载模型快照", recorder, parameters: presetParameters,
      description: "将指定 DeploymentPreset 的模型 Revision 下载到共享 Hugging Face 缓存。该操作使用网络和磁盘；不拉取 Docker 镜像、不启动服务。凭据只从环境变量读取且不会写入 Run Record。",
      async execute(params, context) {
        const preset = await presetFor(params, context);
        context.update(`正在下载 ${preset.model_id}@${preset.model_revision}…`);
        const download = requireSuccess(await runCommand("hf", ["download", preset.model_id, "--revision", preset.model_revision, "--quiet"], { signal: context.signal, timeoutMs: 2 * 60 * 60_000, maxBytes: 2 * 1024 * 1024 }));
        await context.artifact("huggingface-download", `${download.stdout}\n${download.stderr}`);
        if (!(await context.store.snapshotExists(preset))) throw new BittuneError("model_snapshot_missing", "hf download 已完成，但指定 Revision Snapshot 未出现在预期缓存路径。", true);
        return { summary: `已下载模型 Snapshot ${preset.model_id}@${preset.model_revision}。`, provenance_type: "measured", data: { deployment_preset_id: preset.preset_id, deployment_preset_version: preset.version, model_id: preset.model_id, model_revision: preset.model_revision, available: true }, provider: { name: "huggingface-cli" } };
      },
    }),
  ];
}
