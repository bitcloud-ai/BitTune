export type ProvenanceType = "estimated" | "measured" | "derived" | "stored";

export interface ArtifactRef {
  artifact_id: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
  label: string;
}

export interface StableToolError {
  code: string;
  message: string;
  retryable: boolean;
  details?: Record<string, unknown>;
}

export interface Observation<T = unknown> {
  ok: boolean;
  summary: string;
  provenance_type: ProvenanceType;
  measured_at: string;
  run_id: string;
  data?: T;
  warnings: string[];
  artifacts: ArtifactRef[];
  error?: StableToolError;
}

export interface DomainResult<T> {
  summary: string;
  provenance_type: ProvenanceType;
  data: T;
  warnings?: string[];
  provider?: { name: string; version?: string };
  /**
   * Stable, small facts needed to decide the next action. Full `data` remains
   * evidence-store only and must be fetched explicitly by run/artifact ID.
   */
  context_facts?: Record<string, string | number | boolean | null>;
}

export interface ContextProjection {
  run_id?: string;
  tool_name: string;
  status: "completed" | "failed" | "cancelled";
  summary: string;
  facts: Record<string, string | number | boolean | null>;
  warnings: string[];
  artifact_refs: Array<Pick<ArtifactRef, "artifact_id" | "label" | "media_type" | "size_bytes">>;
  /** Bounded structured output for non-recording query/session tools only. */
  query_data?: unknown;
  truncated?: boolean;
  retrieval_hint?: string;
}
