import { mkdir, readdir, readFile } from "node:fs/promises";
import { join } from "node:path";
import { Type } from "typebox";
import { BittuneError } from "../../shared/errors.ts";
import { requireSuccess, runCommand } from "../../shared/process.ts";
import { RunRecorder } from "../../shared/run-recorder.ts";
import { createBittuneTool } from "../../shared/tool.ts";

const Id = Type.String({ pattern: "^[a-z][a-z0-9-]{0,62}$" });
const RunId = Type.String({ pattern: "^run-[a-f0-9-]{36}$" });
const ArtifactId = Type.String({ pattern: "^artifact-[a-f0-9-]{36}$" });

type MetricMap = Record<string, number | string | boolean | null>;

function scalarMetrics(value: unknown, prefix = "", output: MetricMap = {}): MetricMap {
  if (Array.isArray(value)) return output;
  if (value && typeof value === "object") {
    for (const [key, child] of Object.entries(value as Record<string, unknown>)) scalarMetrics(child, prefix ? `${prefix}.${key}` : key, output);
  } else if (["number", "string", "boolean"].includes(typeof value) || value === null) {
    const normalized = prefix.toLowerCase();
    if (/(ttft|tpot|itl|throughput|rps|tps|success|latency|e2e|request|token)/.test(normalized)) output[prefix] = value as number | string | boolean | null;
  }
  return output;
}

function normalizedMetrics(raw: MetricMap): MetricMap {
  const aliases: Array<[string, RegExp]> = [
    ["success_rate", /success.*rate|success_rate/], ["requests_per_second", /(^|\.)rps$|request.*per.*second/],
    ["output_tokens_per_second", /(^|\.)tps$|output.*token.*per.*second|throughput/], ["mean_e2e_latency_ms", /avg.*(latency|e2e)|mean.*(latency|e2e)/],
    ["mean_ttft_ms", /avg.*ttft|mean.*ttft/], ["mean_tpot_ms", /avg.*tpot|mean.*tpot/], ["mean_itl_ms", /avg.*itl|mean.*itl/],
  ];
  const output: MetricMap = { ...raw };
  for (const [target, expression] of aliases) {
    const entry = Object.entries(raw).find(([key]) => expression.test(key.toLowerCase()));
    if (entry) output[target] = entry[1];
  }
  return output;
}

async function findJsonFiles(directory: string, limit = 24): Promise<string[]> {
  try {
    const entries = await readdir(directory, { withFileTypes: true });
    const nested = await Promise.all(entries.map(async (entry) => entry.isDirectory() ? findJsonFiles(join(directory, entry.name), limit) : entry.name.endsWith(".json") ? [join(directory, entry.name)] : []));
    return nested.flat().slice(0, limit);
  } catch { return []; }
}

async function sampleGpuMemory(signal?: AbortSignal): Promise<number | undefined> {
  try {
    const result = await runCommand("nvidia-smi", ["--query-gpu=memory.used", "--format=csv,noheader,nounits"], { signal, timeoutMs: 5_000 });
    if (result.exit_code !== 0) return undefined;
    const values = result.stdout.split(/\r?\n/).map((line) => Number(line.trim())).filter(Number.isFinite);
    return values.length ? Math.max(...values) * 1024 * 1024 : undefined;
  } catch { return undefined; }
}

async function collectEvalscopeMetrics(outputDir: string): Promise<{ metrics: MetricMap; files: Array<{ name: string; content: string }> }> {
  const files = await findJsonFiles(outputDir);
  const parsed = await Promise.all(files.map(async (path) => ({ path, content: await readFile(path, "utf8").catch(() => "") })));
  const metrics = normalizedMetrics(parsed.reduce<MetricMap>((all, file) => {
    try { return scalarMetrics(JSON.parse(file.content), "", all); } catch { return all; }
  }, {}));
  return { metrics, files: parsed.map((item) => ({ name: item.path.slice(outputDir.length + 1), content: item.content })).filter((item) => item.content.length > 0) };
}

export function createBenchmarkTools(recorder: RunRecorder) {
  return [
    createBittuneTool({
      name: "run_performance_test", label: "运行性能压测", recorder,
      parameters: Type.Object({
        instance_id: Id,
        mode: Type.Union([Type.Literal("closed_loop"), Type.Literal("open_loop")]),
        concurrency: Type.Integer({ minimum: 1 }),
        request_count: Type.Integer({ minimum: 1 }),
        input_tokens: Type.Integer({ minimum: 1 }),
        output_tokens: Type.Integer({ minimum: 1 }),
        request_rate: Type.Optional(Type.Number({ exclusiveMinimum: 0 })),
        request_timeout_seconds: Type.Optional(Type.Integer({ minimum: 1 })),
      }),
      description: "对一个明确的 Bittune 受管 vLLM 服务运行一次 EvalScope perf 压测，返回已归一化的吞吐、成功率、E2E、TTFT、TPOT、ITL 与采样显存指标。该操作消耗 GPU；只接受有限领域参数，不接受任意 EvalScope argv、路径或环境变量，不发布 CapacityBaseline。",
      async execute(params, context) {
        const input = params as { instance_id: string; mode: "closed_loop" | "open_loop"; concurrency: number; request_count: number; input_tokens: number; output_tokens: number; request_rate?: number; request_timeout_seconds?: number };
        if (input.mode === "open_loop" && !input.request_rate) throw new BittuneError("invalid_input", "open_loop 压测必须提供 request_rate。", false);
        const service = await context.store.getService(input.instance_id);
        const outputDir = join(context.store.runRoot, context.run_id, "artifacts", "evalscope-output");
        await mkdir(outputDir, { recursive: true });
        const args = [
          "perf", "--url", `${service.endpoint_url}/v1/chat/completions`, "--api", "openai", "--model", service.model_id,
          "--parallel", String(input.concurrency), "--number", String(input.request_count), "--stream", "--enable-progress-tracker",
          "--outputs-dir", outputDir, "--dataset", "random", "--tokenizer-path", service.model_id,
          "--max-tokens", String(input.output_tokens), "--min-tokens", String(input.output_tokens), "--prefix-length", "0",
          "--min-prompt-length", String(input.input_tokens), "--max-prompt-length", String(input.input_tokens), "--extra-args", "{\"ignore_eos\":true}",
          "--total-timeout", String(input.request_timeout_seconds ?? 600),
        ];
        if (input.mode === "open_loop") args.push("--open-loop", "--rate", String(input.request_rate));
        context.update(`正在以 ${input.mode} 模式运行 EvalScope perf（${input.request_count} requests）…`);
        let peakBytes = await sampleGpuMemory(context.signal) ?? 0;
        let sampling = false;
        const sample = async () => {
          if (sampling) return;
          sampling = true;
          try { peakBytes = Math.max(peakBytes, await sampleGpuMemory(context.signal) ?? 0); } finally { sampling = false; }
        };
        const timer = setInterval(() => { void sample(); }, 500);
        let result;
        try {
          result = await runCommand("evalscope", args, { signal: context.signal, timeoutMs: (input.request_timeout_seconds ?? 600) * 1000 + 120_000, maxBytes: 2 * 1024 * 1024 });
        } finally {
          clearInterval(timer);
          await sample();
        }
        await context.artifact("evalscope-cli", `${result!.stdout}\n${result!.stderr}`);
        requireSuccess(result!);
        const collected = await collectEvalscopeMetrics(outputDir);
        for (const file of collected.files) await context.artifact(`evalscope:${file.name}`, file.content, "application/json");
        const summaryArtifact = await context.artifact("evalscope-summary", JSON.stringify({ metrics: collected.metrics, observed_gpu_memory_peak_bytes: peakBytes, requested: input }, null, 2), "application/json");
        const version = await runCommand("evalscope", ["--version"], { signal: context.signal, timeoutMs: 10_000 });
        return { summary: `EvalScope 压测完成：${input.request_count} 个请求，成功率 ${String(collected.metrics.success_rate ?? "未知")}，RPS ${String(collected.metrics.requests_per_second ?? "未知")}。`, provenance_type: "measured", data: { service_instance_id: service.instance_id, deployment_preset_id: service.deployment_preset_id, deployment_preset_version: service.deployment_preset_version, model_id: service.model_id, model_revision: service.model_revision, runtime_kind: service.runtime_kind, runtime_image_digest: service.runtime_image_digest, runtime_capability_version: service.runtime_capability_version, serving_configuration: service.serving_configuration, requested: input, metrics: collected.metrics, observed_gpu_memory_peak_bytes: peakBytes, summary_artifact_id: summaryArtifact.artifact_id }, warnings: Object.keys(collected.metrics).length ? [] : ["未在 EvalScope JSON 输出中识别到标准指标；请用 analyze_benchmark_artifact 检查原始报告。"], provider: { name: "evalscope", ...(version.exit_code === 0 ? { version: version.stdout.trim() } : {}) } };
      },
    }),
    createBittuneTool({
      name: "analyze_benchmark_artifact", label: "分析压测证据", recorder,
      parameters: Type.Object({ benchmark_run_id: RunId, artifact_id: ArtifactId }),
      description: "读取明确 benchmark_run_id + artifact_id 指向的已登记 EvalScope Artifact，归一并解释 TTFT、TPOT、ITL、吞吐、成功率和错误线索。只接受 Run Store 中的 ArtifactRef；不接受任意路径、不重新压测，也不发布 CapacityBaseline。",
      async execute(params, context) {
        const input = params as { benchmark_run_id: string; artifact_id: string };
        const run = await context.store.getRun(input.benchmark_run_id);
        if (run.manifest.tool_name !== "run_performance_test") throw new BittuneError("invalid_input", "benchmark_run_id 必须是 run_performance_test 的 Run Record。", false);
        const artifact = await context.store.readArtifact(input.benchmark_run_id, input.artifact_id, 0, 65_536);
        let parsed: unknown;
        try { parsed = JSON.parse(artifact.text); } catch { parsed = { raw_excerpt: artifact.text }; }
        const metrics = normalizedMetrics(scalarMetrics(parsed));
        const warnings = artifact.truncated ? ["Artifact 已按 64 KiB 上限截断；完整内容仍保存在受管 Run Store。"] : [];
        return { summary: `已分析 EvalScope Artifact：成功率 ${String(metrics.success_rate ?? "未识别")}，RPS ${String(metrics.requests_per_second ?? "未识别")}，TTFT ${String(metrics.mean_ttft_ms ?? "未识别")}。`, provenance_type: "derived", data: { benchmark_run_id: input.benchmark_run_id, artifact_id: input.artifact_id, metrics, total_artifact_bytes: artifact.total_bytes }, warnings, provider: { name: "evalscope-artifact-analyzer" } };
      },
    }),
  ];
}
