import { Type } from "typebox";
import { RunRecorder } from "../../shared/run-recorder.ts";
import { createBittuneTool } from "../../shared/tool.ts";

const RunId = Type.String({ pattern: "^run-[a-f0-9-]{36}$" });
const ArtifactId = Type.String({ pattern: "^artifact-[a-f0-9-]{36}$" });

export function createEvidenceTools(recorder: RunRecorder) {
  return [
    createBittuneTool({
      name: "list_run_records", label: "列出运行记录", recorder,
      parameters: Type.Object({ tool_name: Type.Optional(Type.String({ minLength: 1, maxLength: 80 })), status: Type.Optional(Type.Union([Type.Literal("completed"), Type.Literal("failed")])), limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 100 })) }),
      description: "列出 Bittune Run Record 摘要，可按 Tool 名称、状态和数量过滤。用于跨 Session 查找历史证据；只读取保存记录，不重新执行任何 Provider。",
      async execute(params, context) {
        const input = params as { tool_name?: string; status?: "completed" | "failed"; limit?: number };
        const records = await context.store.listRuns(input);
        return { summary: `找到 ${records.length} 条 Run Record。`, provenance_type: "stored", data: { records }, provider: { name: "file-run-store" } };
      },
    }),
    createBittuneTool({
      name: "get_run_record", label: "读取运行记录", recorder, parameters: Type.Object({ run_id: RunId }),
      description: "读取一个明确 Run ID 的结构化 Run Record、Observation 和 ArtifactRef。用于解释、复核或关联历史事实；只读，不重跑该操作。",
      async execute(params, context) {
        const input = params as { run_id: string };
        const record = await context.store.getRun(input.run_id);
        return { summary: `读取 Run Record ${input.run_id}。`, provenance_type: "stored", data: record, provider: { name: "file-run-store" } };
      },
    }),
    createBittuneTool({
      name: "read_artifact_excerpt", label: "读取证据片段", recorder,
      parameters: Type.Object({ run_id: RunId, artifact_id: ArtifactId, offset_bytes: Type.Optional(Type.Integer({ minimum: 0 })), max_bytes: Type.Optional(Type.Integer({ minimum: 1, maximum: 65_536 })) }),
      description: "从一个明确 Run ID 与 Artifact ID 读取有边界的证据片段。用于检查日志或大报告；不接受任意本地路径，最大读取 64 KiB，不改变状态。",
      async execute(params, context) {
        const input = params as { run_id: string; artifact_id: string; offset_bytes?: number; max_bytes?: number };
        const excerpt = await context.store.readArtifact(input.run_id, input.artifact_id, input.offset_bytes ?? 0, input.max_bytes ?? 16_384);
        return { summary: `读取 Artifact ${input.artifact_id} 的 ${excerpt.text.length} 字节片段。`, provenance_type: "stored", data: { run_id: input.run_id, artifact_id: input.artifact_id, ...excerpt }, provider: { name: "bounded-artifact-reader" } };
      },
    }),
  ];
}
