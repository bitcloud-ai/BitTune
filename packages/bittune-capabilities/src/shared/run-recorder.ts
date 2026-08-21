import type { AgentToolResult, AgentToolUpdateCallback, ExtensionContext } from "../../../bittune-runtime/src/core/extensions/types.ts";
import { asBittuneError } from "./errors.ts";
import type { ArtifactRef, ContextProjection, DomainResult, Observation, ProvenanceType } from "./observation.ts";
import { redact } from "./redaction.ts";
import { StateStore, type RunStatus } from "./state-store.ts";

export type RecordingPolicy = "command" | "evidence" | "query" | "session";

export interface ToolRunContext {
  run_id: string;
  store: StateStore;
  signal?: AbortSignal | undefined;
  update: (message: string) => void;
  artifact: (label: string, content: string | Uint8Array, mediaType?: string) => Promise<ArtifactRef>;
}

interface QueryRunContext extends ToolRunContext {
  run_id: string;
}

const MAX_SUMMARY_CHARS = 1_600;
const MAX_FACTS = 40;
const MAX_WARNINGS = 8;
const MAX_WARNING_CHARS = 240;
const MAX_ARTIFACT_REFS = 12;
const MAX_QUERY_ITEMS = 32;
const MAX_QUERY_DEPTH = 4;
const MAX_QUERY_STRING_CHARS = 1_000;

function clip(value: string, limit: number): { text: string; truncated: boolean } {
  return value.length <= limit ? { text: value, truncated: false } : { text: `${value.slice(0, limit)}...`, truncated: true };
}

function scalarFacts(value: unknown, limit = MAX_FACTS): Record<string, string | number | boolean | null> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const facts: Record<string, string | number | boolean | null> = {};
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    if (Object.keys(facts).length >= limit) break;
    if (item === null || typeof item === "string" || typeof item === "number" || typeof item === "boolean") facts[key] = item;
  }
  return facts;
}

function boundedQueryData(value: unknown, depth = 0): unknown {
  if (value === null || typeof value === "number" || typeof value === "boolean") return value;
  if (typeof value === "string") return clip(value, MAX_QUERY_STRING_CHARS).text;
  if (depth >= MAX_QUERY_DEPTH) return "[omitted: nesting limit]";
  if (Array.isArray(value)) {
    const values = value.slice(0, MAX_QUERY_ITEMS).map((item) => boundedQueryData(item, depth + 1));
    return value.length > values.length ? [...values, `[${value.length - values.length} more items omitted]`] : values;
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>).slice(0, MAX_QUERY_ITEMS).map(([key, item]) => [key, boundedQueryData(item, depth + 1)]));
  }
  return String(value);
}

function projection(args: {
  toolName: string;
  runId?: string;
  status: Exclude<RunStatus, "running" | "incomplete">;
  observation: Observation;
  facts?: Record<string, string | number | boolean | null>;
  queryData?: unknown;
}): ContextProjection {
  const summary = clip(args.observation.summary, MAX_SUMMARY_CHARS);
  const warnings = args.observation.warnings.slice(0, MAX_WARNINGS).map((warning) => clip(warning, MAX_WARNING_CHARS).text);
  const artifacts = args.observation.artifacts.slice(0, MAX_ARTIFACT_REFS).map(({ artifact_id, label, media_type, size_bytes }) => ({ artifact_id, label, media_type, size_bytes }));
  const truncated = summary.truncated || args.observation.warnings.length > warnings.length || args.observation.artifacts.length > artifacts.length;
  return {
    ...(args.runId ? { run_id: args.runId } : {}),
    tool_name: args.toolName,
    status: args.status,
    summary: summary.text,
    facts: { ...scalarFacts(args.observation.data), ...(args.facts ?? {}) },
    warnings,
    artifact_refs: artifacts,
    ...(args.queryData === undefined ? {} : { query_data: boundedQueryData(args.queryData) }),
    ...(truncated ? { truncated: true } : {}),
    ...(args.runId ? { retrieval_hint: `Use get_run_record or read_artifact_excerpt with run_id ${args.runId} for evidence details.` } : {}),
  };
}

/**
 * Centralizes the durable boundary. Query/session tools deliberately bypass it:
 * their calls still live in Session JSONL but do not recursively create evidence.
 */
export class RunRecorder {
  constructor(private readonly store: StateStore) {}

  async execute<T>(
    policy: RecordingPolicy,
    toolName: string,
    toolCallId: string,
    input: unknown,
    signal: AbortSignal | undefined,
    onUpdate: AgentToolUpdateCallback | undefined,
    extensionContext: ExtensionContext,
    operation: (context: ToolRunContext) => Promise<DomainResult<T>>,
  ): Promise<AgentToolResult<unknown>> {
    try {
      const sessionId = extensionContext.sessionManager?.getSessionId?.() ?? "external";
      if (policy === "query" || policy === "session") return await this.executeTransient(toolName, input, signal, onUpdate, operation);

      await this.store.initialize();

      const started = await this.store.startRun(toolName, input, { session_id: sessionId, tool_call_id: toolCallId });
      const runId = started.run_id;
      const artifacts: ArtifactRef[] = [];
      const runContext: ToolRunContext = {
        run_id: runId,
        store: this.store,
        signal,
        update: (message) => onUpdate?.({ content: [{ type: "text", text: message }], details: {} }),
        artifact: async (label, content, mediaType) => {
          const artifact = await this.store.writeArtifact(runId, label, content, mediaType);
          artifacts.push(artifact);
          return artifact;
        },
      };
      let observation: Observation<T>;
      let provider: DomainResult<T>["provider"] | undefined;
      let facts: DomainResult<T>["context_facts"] | undefined;
      let status: Exclude<RunStatus, "running" | "incomplete"> = "completed";
      try {
        const result = await operation(runContext);
        provider = result.provider;
        facts = result.context_facts;
        observation = { ok: true, summary: result.summary, provenance_type: result.provenance_type, measured_at: new Date().toISOString(), run_id: runId, data: result.data, warnings: result.warnings ?? [], artifacts };
      } catch (error) {
        const stable = asBittuneError(error).stable;
        status = stable.code === "cancelled" ? "cancelled" : "failed";
        observation = { ok: false, summary: stable.message, provenance_type: "estimated", measured_at: new Date().toISOString(), run_id: runId, warnings: [], artifacts, error: stable };
      }
      await this.store.finishRun(runId, { tool_name: toolName, started_at: started.started_at, status, provenance_type: observation.provenance_type, input, ...(provider ? { provider } : {}), artifacts, observation });
      const result = projection({ toolName, runId, status, observation, facts });
      return { content: [{ type: "text", text: JSON.stringify(result) }], details: { run_id: runId, projection: result } };
    } finally {
      this.store.close();
    }
  }

  private async executeTransient<T>(toolName: string, input: unknown, signal: AbortSignal | undefined, onUpdate: AgentToolUpdateCallback | undefined, operation: (context: ToolRunContext) => Promise<DomainResult<T>>): Promise<AgentToolResult<unknown>> {
    const context: QueryRunContext = {
      run_id: "",
      store: this.store,
      signal,
      update: (message) => onUpdate?.({ content: [{ type: "text", text: message }], details: {} }),
      artifact: async () => { throw new Error(`${toolName} is a non-recording tool and cannot create artifacts.`); },
    };
    try {
      const result = await operation(context);
      const observation: Observation<T> = { ok: true, summary: result.summary, provenance_type: result.provenance_type, measured_at: new Date().toISOString(), run_id: "", data: redact(result.data) as T, warnings: result.warnings ?? [], artifacts: [] };
      const projected = projection({ toolName, status: "completed", observation, facts: result.context_facts, queryData: redact(result.data) });
      return { content: [{ type: "text", text: JSON.stringify(projected) }], details: { projection: projected } };
    } catch (error) {
      const stable = asBittuneError(error).stable;
      const status = stable.code === "cancelled" ? "cancelled" : "failed";
      const observation: Observation = { ok: false, summary: stable.message, provenance_type: "estimated", measured_at: new Date().toISOString(), run_id: "", warnings: [], artifacts: [], error: stable };
      const projected = projection({ toolName, status, observation });
      return { content: [{ type: "text", text: JSON.stringify(projected) }], details: { projection: projected } };
    }
  }
}
