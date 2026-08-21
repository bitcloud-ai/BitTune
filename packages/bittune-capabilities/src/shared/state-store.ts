import { randomUUID, createHash } from "node:crypto";
import { mkdir, readdir, readFile, rename, stat, writeFile } from "node:fs/promises";
import { existsSync, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { delimiter, join, relative, resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { BittuneError } from "./errors.ts";
import { canonicalJson, requireId, requireVersion, sha256 } from "./identifiers.ts";
import type { ArtifactRef, Observation, ProvenanceType } from "./observation.ts";
import { pathInside } from "./atomic-files.ts";
import { redact, redactText } from "./redaction.ts";
import { materializeVllmConfiguration, type ServingConfiguration } from "./vllm-capabilities.ts";

/** Domain-record shape is unchanged so existing references remain valid. */
export const STATE_SCHEMA_VERSION = 2;
const DEFAULT_NAMESPACE = "default";

export interface DeploymentPreset {
  schema_version: number;
  preset_id: string;
  version: string;
  model_id: string;
  model_revision: string;
  artifact_fingerprint?: string;
  runtime_kind: "vllm";
  runtime_image_repository: string;
  runtime_image_digest: string;
  serving_configuration: ServingConfiguration;
  source_run_ids: string[];
  created_at: string;
  source: "published";
  content_hash: string;
}

export interface MeasuredOperatingPoint {
  deployment_fingerprint: string;
  environment_fingerprint: string;
  workload_fingerprint: string;
  serving_configuration: ServingConfiguration;
  configuration_fingerprint: string;
  metrics: Record<string, unknown>;
  gates: Record<string, unknown>;
  source_run_ids: string[];
  measured_at: string;
  verification_status: "verified" | "validation_required" | "unverified";
}

export interface CapacityBaseline {
  schema_version: number;
  baseline_id: string;
  version: string;
  deployment_preset_id: string;
  deployment_preset_version: string;
  operating_points: MeasuredOperatingPoint[];
  capacity_envelope?: { search_strategy: string; boundary_evidence_run_ids: string[]; workload_fingerprint: string; configuration_fingerprint: string };
  source_run_ids: string[];
  candidate_run_id: string;
  candidate_hash: string;
  created_at: string;
  content_hash: string;
}

export interface ServiceObservation {
  desired_state: "running" | "stopped" | "unknown";
  observed_state: string;
  observed_at: string;
  observation_run_id?: string;
  freshness: "fresh" | "stale" | "unknown";
  state_generation: number;
}

export interface ServiceManifest {
  schema_version: number;
  instance_id: string;
  service_name: string;
  container_id: string;
  container_name: string;
  deployment_preset_id: string;
  deployment_preset_version: string;
  model_id: string;
  model_revision: string;
  runtime_kind: "vllm";
  runtime_image_repository: string;
  runtime_image_digest: string;
  runtime_capability_version: string;
  runtime_cli_profile?: { version?: string; supported_flags: string[]; supports_swap_space: boolean };
  runtime_engine_args?: string[];
  endpoint_url: string;
  port: number;
  serving_configuration: ServingConfiguration;
  config_hash: string;
  created_at: string;
  /** Compatibility field. Prefer observation.observed_state and freshness. */
  last_known_status: string;
  observation?: ServiceObservation;
  owner?: { kind: "experiment"; experiment_id: string; experiment_version: string };
}

export interface ExperimentWorkload { mode: "closed_loop" | "open_loop"; concurrency: number; request_count: number; input_tokens: number; output_tokens: number; request_rate?: number; request_timeout_seconds?: number; }
export interface ExperimentSpec {
  schema_version: number; experiment_id: string; version: string; deployment_preset_id: string; deployment_preset_version: string; reference_service_instance_id: string; reference_service_configuration: ServingConfiguration; runtime_capability_version: string; experiment_kind: "runtime_tuning" | "capacity_exploration"; baseline_environment_run_id: string; objective_metric: "requests_per_second" | "output_tokens_per_second"; objective_target?: number;
  constraints: { min_success_rate?: number; max_mean_ttft_ms?: number; max_mean_e2e_latency_ms?: number; max_gpu_memory_bytes?: number };
  workload: ExperimentWorkload; allowed_workload?: { concurrency?: { minimum: number; maximum: number }; request_rate?: { minimum: number; maximum: number } }; allowed_parameters: Record<string, { minimum?: number; maximum?: number; values?: Array<boolean | number | string> }>;
  budget: { max_trials: number; max_total_duration_seconds: number; max_failures: number }; minimum_improvement_percent: number; required_repetitions: number; created_at: string; content_hash: string;
}
export type TrialResultClass = "valid" | "constraint_failed" | "failed" | "invalid_environment_drift" | "invalid_resource_contention";
export interface ExperimentTrial { schema_version: number; trial_id: string; experiment_id: string; experiment_version: string; configuration: ServingConfiguration; configuration_hash: string; workload: ExperimentWorkload; pre_environment_run_id: string; post_environment_run_id: string; service_run_id: string; probe_run_id: string; benchmark_run_id: string; result_class: TrialResultClass; constraint_failures: string[]; metrics: Record<string, unknown>; evidence_duration_seconds: number; resource_contention?: { baseline_processes: number; trial_processes: number; reason: string }; created_at: string; content_hash: string; }
export interface ExperimentComparison { schema_version: number; comparison_id: string; experiment_id: string; experiment_version: string; trial_ids: string[]; valid_trial_ids: string[]; ranking: Array<{ configuration_hash: string; trial_ids: string[]; objective_mean: number; repetitions: number; stable: boolean }>; best_configuration_hash?: string; best_trial_ids: string[]; status: "objective_met" | "no_gain" | "budget_exhausted" | "unsafe" | "cancelled" | "in_progress" | "validation_required"; reason: string; candidate_run_id: string; candidate_hash: string; created_at: string; content_hash: string; }

export type RunStatus = "running" | "completed" | "failed" | "cancelled" | "incomplete";
export interface RunManifest {
  schema_version: number;
  run_id: string;
  namespace_id: string;
  session_id: string;
  tool_call_id: string;
  parent_run_id?: string;
  source_run_ids?: string[];
  tool_name: string;
  started_at: string;
  finished_at: string;
  status: RunStatus;
  provenance_type: ProvenanceType;
  input_hash: string;
  input: unknown;
  provider?: { name: string; version?: string };
  observation_hash: string;
  artifacts: ArtifactRef[];
}

type StartRunOptions = { session_id?: string; tool_call_id?: string; parent_run_id?: string; source_run_ids?: string[] };
type FinishRunArgs = {
  tool_name: string; started_at: string; status: Exclude<RunStatus, "running" | "incomplete">; provenance_type: ProvenanceType;
  input: unknown; provider?: { name: string; version?: string }; artifacts: ArtifactRef[]; observation: Observation; parent_run_id?: string; source_run_ids?: string[];
};
type JsonRow = { record_json: string };
type RunRow = {
  run_id: string; namespace_id: string; session_id: string; tool_call_id: string; parent_run_id: string | null; source_run_ids_json: string | null;
  tool_name: string; started_at: string; finished_at: string | null; status: RunStatus; provenance_type: ProvenanceType; input_json: string; input_hash: string; provider_json: string | null; observation_hash: string | null;
};
type SharedDatabase = { database: DatabaseSync; references: number };

const OPEN_DATABASES = new Map<string, SharedDatabase>();

function json(value: unknown): string { return JSON.stringify(value); }
function parse<T>(value: string): T { return JSON.parse(value) as T; }
function now(): string { return new Date().toISOString(); }
function isTextualMediaType(mediaType: string): boolean { return mediaType.startsWith("text/") || mediaType === "application/json" || mediaType.endsWith("+json"); }
function defaultStateObservation(status: string): ServiceObservation {
  return { desired_state: status === "stopped" ? "stopped" : status === "running" ? "running" : "unknown", observed_state: status, observed_at: now(), freshness: "unknown", state_generation: 0 };
}

/** A stable, opaque namespace for one local project/workspace. */
export function namespaceForWorkspace(cwd?: string): string {
  if (!cwd) return process.env.BITTUNE_NAMESPACE?.trim() || DEFAULT_NAMESPACE;
  const digest = createHash("sha256").update(resolve(cwd)).digest("hex").slice(0, 20);
  return `workspace-${digest}`;
}

export class StateStore {
  readonly root: string;
  readonly namespaceId: string;
  private dbInstance: DatabaseSync | undefined;
  private initialized = false;
  private initializing: Promise<void> | undefined;

  constructor(root = process.env.BITTUNE_STATE_DIR || join(homedir(), ".bittune", "state"), namespaceId = process.env.BITTUNE_NAMESPACE || DEFAULT_NAMESPACE) {
    this.root = resolve(root);
    this.namespaceId = namespaceId;
    mkdirSync(this.root, { recursive: true, mode: 0o700 });
  }

  private get db(): DatabaseSync {
    if (!this.dbInstance) {
      let shared = OPEN_DATABASES.get(this.root);
      if (!shared) {
        const database = new DatabaseSync(join(this.root, "bittune.db"));
        database.exec("PRAGMA foreign_keys = ON; PRAGMA journal_mode = WAL; PRAGMA synchronous = FULL; PRAGMA busy_timeout = 5000;");
        this.createSchema(database);
        shared = { database, references: 0 };
        OPEN_DATABASES.set(this.root, shared);
      }
      shared.references += 1;
      this.dbInstance = shared.database;
    }
    return this.dbInstance;
  }

  /** Callers that own a short-lived Store can release Windows file handles. */
  close(): void {
    const database = this.dbInstance;
    if (!database) return;
    this.dbInstance = undefined;
    const shared = OPEN_DATABASES.get(this.root);
    if (!shared || shared.database !== database) {
      database.close();
      return;
    }
    shared.references -= 1;
    if (shared.references > 0) return;
    try {
      if (process.platform !== "win32") database.exec("PRAGMA wal_checkpoint(TRUNCATE);");
    } catch {
      // A concurrent writer can prevent checkpointing; close still releases
      // this connection and the next open will recover the WAL safely.
    }
    database.close();
    OPEN_DATABASES.delete(this.root);
  }

  /** Working directory for run-scoped raw Provider output before it is registered as an Artifact (e.g. EvalScope JSON). */
  get runRoot(): string { return join(this.root, "runs"); }
  get artifactRoot(): string { return join(this.root, "artifacts"); }
  get hfCacheRoot(): string { return process.env.HF_HOME || join(homedir(), ".cache", "huggingface"); }
  get hfCacheRoots(): string[] {
    const configured = process.env.BITTUNE_MODEL_CACHE_ROOTS?.split(delimiter).map((item) => item.trim()).filter(Boolean) ?? [];
    return Array.from(new Set([this.hfCacheRoot, ...configured].map((root) => resolve(root))));
  }

  async initialize(): Promise<void> {
    if (this.initialized) return;
    if (!this.initializing) this.initializing = this.initializeOnce();
    await this.initializing;
  }

  private async initializeOnce(): Promise<void> {
    await mkdir(this.artifactRoot, { recursive: true, mode: 0o700 });
    this.db.prepare("INSERT OR IGNORE INTO namespaces(namespace_id, created_at) VALUES (?, ?)").run(this.namespaceId, now());
    this.initialized = true;
  }

  async listPresets(): Promise<DeploymentPreset[]> {
    await this.initialize();
    return this.records<DeploymentPreset>("SELECT record_json FROM deployment_presets WHERE namespace_id = ? ORDER BY preset_id, version_number", this.namespaceId);
  }

  async getPreset(presetId: string, version?: string): Promise<DeploymentPreset> {
    await this.initialize(); requireId(presetId, "preset_id"); if (version) requireVersion(version);
    const row = version
      ? this.db.prepare("SELECT record_json FROM deployment_presets WHERE namespace_id = ? AND preset_id = ? AND version = ?").get(this.namespaceId, presetId, version) as JsonRow | undefined
      : this.db.prepare("SELECT record_json FROM deployment_presets WHERE namespace_id = ? AND preset_id = ? ORDER BY version_number DESC LIMIT 1").get(this.namespaceId, presetId) as JsonRow | undefined;
    if (!row) throw new BittuneError("preset_not_found", `找不到 DeploymentPreset ${presetId}${version ? `/${version}` : ""}。`);
    return parse<DeploymentPreset>(row.record_json);
  }

  async publishPreset(input: Omit<DeploymentPreset, "schema_version" | "version" | "created_at" | "source" | "content_hash"> & { expected_parent_version?: string }): Promise<DeploymentPreset> {
    await this.initialize(); requireId(input.preset_id, "preset_id");
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]*(\/[A-Za-z0-9][A-Za-z0-9._-]*)?$/.test(input.model_id) || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$/.test(input.model_revision)) throw new BittuneError("invalid_input", "model_id or model_revision format is invalid.", false);
    if (input.runtime_kind !== "vllm" || !/^[a-z0-9][a-z0-9._:/-]{0,255}$/.test(input.runtime_image_repository) || !/^sha256:[a-f0-9]{64}$/.test(input.runtime_image_digest)) throw new BittuneError("invalid_input", "runtime identity format is invalid.", false);
    const sourceRunIds = [...new Set(input.source_run_ids)];
    if (sourceRunIds.some((id) => !/^run-[a-f0-9-]{36}$/.test(id))) throw new BittuneError("invalid_input", "source_run_ids contains an invalid Run ID.", false);
    return this.versionedWrite("deployment-presets", input.preset_id, async (version) => {
      const { expected_parent_version: _parent, ...content } = input;
      const record: DeploymentPreset = { ...content, serving_configuration: materializeVllmConfiguration(input.serving_configuration), source_run_ids: sourceRunIds, schema_version: STATE_SCHEMA_VERSION, version, created_at: now(), source: "published", content_hash: "" };
      record.content_hash = sha256(canonicalJson({ ...record, content_hash: undefined }));
      this.db.prepare("INSERT INTO deployment_presets(namespace_id, preset_id, version, version_number, record_json) VALUES (?, ?, ?, ?, ?)").run(this.namespaceId, record.preset_id, record.version, this.versionNumber(record.version), json(record));
      return record;
    }, input.expected_parent_version);
  }

  async listBaselines(): Promise<CapacityBaseline[]> { await this.initialize(); return this.records<CapacityBaseline>("SELECT record_json FROM capacity_baselines WHERE namespace_id = ? ORDER BY baseline_id, version_number", this.namespaceId); }
  async getBaseline(baselineId: string, version?: string): Promise<CapacityBaseline> {
    await this.initialize(); requireId(baselineId, "baseline_id"); if (version) requireVersion(version);
    const row = version
      ? this.db.prepare("SELECT record_json FROM capacity_baselines WHERE namespace_id = ? AND baseline_id = ? AND version = ?").get(this.namespaceId, baselineId, version) as JsonRow | undefined
      : this.db.prepare("SELECT record_json FROM capacity_baselines WHERE namespace_id = ? AND baseline_id = ? ORDER BY version_number DESC LIMIT 1").get(this.namespaceId, baselineId) as JsonRow | undefined;
    if (!row) throw new BittuneError("baseline_not_found", `找不到 CapacityBaseline ${baselineId}${version ? `/${version}` : ""}。`);
    return parse<CapacityBaseline>(row.record_json);
  }
  async publishBaseline(record: Omit<CapacityBaseline, "schema_version" | "version" | "created_at" | "content_hash">): Promise<CapacityBaseline> {
    await this.initialize(); requireId(record.baseline_id, "baseline_id");
    return this.versionedWrite("capacity-baselines", record.baseline_id, async (version) => {
      const stored: CapacityBaseline = { ...record, schema_version: STATE_SCHEMA_VERSION, version, created_at: now(), content_hash: "" };
      stored.content_hash = sha256(canonicalJson({ ...stored, content_hash: undefined }));
      this.db.prepare("INSERT INTO capacity_baselines(namespace_id, baseline_id, version, version_number, record_json) VALUES (?, ?, ?, ?, ?)").run(this.namespaceId, stored.baseline_id, stored.version, this.versionNumber(stored.version), json(stored));
      return stored;
    });
  }

  async saveService(manifest: ServiceManifest): Promise<void> {
    await this.initialize(); requireId(manifest.instance_id, "instance_id");
    if (manifest.schema_version !== STATE_SCHEMA_VERSION) throw new BittuneError("state_schema_mismatch", "Service manifest uses an unsupported schema; initialize a new State Store.", false);
    const record = this.normalizeService(manifest);
    this.db.prepare("INSERT INTO services(namespace_id, instance_id, record_json) VALUES (?, ?, ?) ON CONFLICT(namespace_id, instance_id) DO UPDATE SET record_json = excluded.record_json").run(this.namespaceId, record.instance_id, json(record));
  }
  async getService(instanceId: string): Promise<ServiceManifest> {
    await this.initialize(); requireId(instanceId, "instance_id");
    const row = this.db.prepare("SELECT record_json FROM services WHERE namespace_id = ? AND instance_id = ?").get(this.namespaceId, instanceId) as JsonRow | undefined;
    if (!row) throw new BittuneError("service_not_found", `找不到受管服务 ${instanceId}。`);
    return this.normalizeService(parse<ServiceManifest>(row.record_json));
  }
  async listServices(): Promise<ServiceManifest[]> { await this.initialize(); return this.records<ServiceManifest>("SELECT record_json FROM services WHERE namespace_id = ? ORDER BY instance_id", this.namespaceId).map((item) => this.normalizeService(item)); }
  async markServicesStale(reason = "unmanaged host operation may have changed state"): Promise<void> {
    await this.initialize();
    const services = await this.listServices();
    for (const service of services) {
      const observation = service.observation ?? defaultStateObservation(service.last_known_status);
      void reason;
      await this.saveService({ ...service, observation: { ...observation, freshness: "stale", observed_at: now(), state_generation: observation.state_generation + 1 } });
    }
  }
  async recordServiceObservation(instanceId: string, observedState: string, runId: string | undefined, desiredState?: ServiceObservation["desired_state"]): Promise<ServiceManifest> {
    const service = await this.getService(instanceId);
    const previous = service.observation ?? defaultStateObservation(service.last_known_status);
    const updated = this.normalizeService({ ...service, last_known_status: observedState, observation: { desired_state: desiredState ?? previous.desired_state, observed_state: observedState, observed_at: now(), ...(runId ? { observation_run_id: runId } : {}), freshness: runId ? "fresh" : "unknown", state_generation: previous.state_generation + 1 } });
    await this.saveService(updated); return updated;
  }

  async listExperimentSpecs(): Promise<ExperimentSpec[]> { await this.initialize(); return this.records<ExperimentSpec>("SELECT record_json FROM experiment_specs WHERE namespace_id = ? ORDER BY experiment_id, version_number", this.namespaceId); }
  async getExperimentSpec(experimentId: string, version?: string): Promise<ExperimentSpec> {
    await this.initialize(); requireId(experimentId, "experiment_id"); if (version) requireVersion(version);
    const row = version
      ? this.db.prepare("SELECT record_json FROM experiment_specs WHERE namespace_id = ? AND experiment_id = ? AND version = ?").get(this.namespaceId, experimentId, version) as JsonRow | undefined
      : this.db.prepare("SELECT record_json FROM experiment_specs WHERE namespace_id = ? AND experiment_id = ? ORDER BY version_number DESC LIMIT 1").get(this.namespaceId, experimentId) as JsonRow | undefined;
    if (!row) throw new BittuneError("experiment_not_found", `找不到 ExperimentSpec ${experimentId}${version ? `/${version}` : ""}。`);
    return parse<ExperimentSpec>(row.record_json);
  }
  async publishExperimentSpec(input: Omit<ExperimentSpec, "schema_version" | "version" | "created_at" | "content_hash"> & { expected_parent_version?: string }): Promise<ExperimentSpec> {
    await this.initialize(); requireId(input.experiment_id, "experiment_id");
    return this.versionedWrite("experiment-specs", input.experiment_id, async (version) => {
      const { expected_parent_version: _parent, ...content } = input;
      const stored: ExperimentSpec = { ...content, schema_version: STATE_SCHEMA_VERSION, version, created_at: now(), content_hash: "" };
      stored.content_hash = sha256(canonicalJson({ ...stored, content_hash: undefined }));
      this.db.prepare("INSERT INTO experiment_specs(namespace_id, experiment_id, version, version_number, record_json) VALUES (?, ?, ?, ?, ?)").run(this.namespaceId, stored.experiment_id, stored.version, this.versionNumber(stored.version), json(stored));
      return stored;
    }, input.expected_parent_version);
  }
  async saveExperimentTrial(record: Omit<ExperimentTrial, "schema_version" | "created_at" | "content_hash">): Promise<ExperimentTrial> {
    await this.initialize(); requireId(record.trial_id, "trial_id"); requireId(record.experiment_id, "experiment_id"); requireVersion(record.experiment_version);
    const stored: ExperimentTrial = { ...record, schema_version: STATE_SCHEMA_VERSION, created_at: now(), content_hash: "" };
    stored.content_hash = sha256(canonicalJson({ ...stored, content_hash: undefined }));
    this.db.prepare("INSERT INTO experiment_trials(namespace_id, experiment_id, trial_id, record_json) VALUES (?, ?, ?, ?)").run(this.namespaceId, stored.experiment_id, stored.trial_id, json(stored));
    return stored;
  }
  async listExperimentTrials(experimentId: string): Promise<ExperimentTrial[]> { await this.initialize(); requireId(experimentId, "experiment_id"); return this.records<ExperimentTrial>("SELECT record_json FROM experiment_trials WHERE namespace_id = ? AND experiment_id = ? ORDER BY rowid", this.namespaceId, experimentId); }
  async getExperimentTrial(experimentId: string, trialId: string): Promise<ExperimentTrial> {
    await this.initialize(); requireId(experimentId, "experiment_id"); requireId(trialId, "trial_id");
    const row = this.db.prepare("SELECT record_json FROM experiment_trials WHERE namespace_id = ? AND experiment_id = ? AND trial_id = ?").get(this.namespaceId, experimentId, trialId) as JsonRow | undefined;
    if (!row) throw new BittuneError("trial_not_found", `找不到 Trial ${trialId}。`);
    const record = parse<ExperimentTrial>(row.record_json); this.ensureSchema(record); return record;
  }
  async saveExperimentComparison(record: Omit<ExperimentComparison, "schema_version" | "created_at" | "content_hash">): Promise<ExperimentComparison> {
    await this.initialize(); requireId(record.comparison_id, "comparison_id"); requireId(record.experiment_id, "experiment_id"); requireVersion(record.experiment_version);
    const stored: ExperimentComparison = { ...record, schema_version: STATE_SCHEMA_VERSION, created_at: now(), content_hash: "" };
    stored.content_hash = sha256(canonicalJson({ ...stored, content_hash: undefined }));
    this.db.prepare("INSERT INTO experiment_comparisons(namespace_id, experiment_id, comparison_id, record_json) VALUES (?, ?, ?, ?)").run(this.namespaceId, stored.experiment_id, stored.comparison_id, json(stored)); return stored;
  }
  async listExperimentComparisons(experimentId: string): Promise<ExperimentComparison[]> { await this.initialize(); requireId(experimentId, "experiment_id"); return this.records<ExperimentComparison>("SELECT record_json FROM experiment_comparisons WHERE namespace_id = ? AND experiment_id = ? ORDER BY rowid DESC", this.namespaceId, experimentId); }
  async getExperimentComparison(experimentId: string, comparisonId: string): Promise<ExperimentComparison> {
    await this.initialize(); requireId(experimentId, "experiment_id"); requireId(comparisonId, "comparison_id");
    const row = this.db.prepare("SELECT record_json FROM experiment_comparisons WHERE namespace_id = ? AND experiment_id = ? AND comparison_id = ?").get(this.namespaceId, experimentId, comparisonId) as JsonRow | undefined;
    if (!row) throw new BittuneError("comparison_not_found", `找不到 Comparison ${comparisonId}。`);
    const record = parse<ExperimentComparison>(row.record_json); this.ensureSchema(record); return record;
  }

  async startRun(toolName: string, input: unknown, options: StartRunOptions = {}): Promise<{ run_id: string; started_at: string }> {
    await this.initialize();
    const runId = `run-${randomUUID()}`; const startedAt = now(); const sanitizedInput = redact(input);
    this.db.prepare("INSERT INTO runs(run_id, namespace_id, session_id, tool_call_id, parent_run_id, source_run_ids_json, tool_name, started_at, status, provenance_type, input_json, input_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', 'estimated', ?, ?)").run(
      runId, this.namespaceId, options.session_id ?? "external", options.tool_call_id ?? "external", options.parent_run_id ?? null, options.source_run_ids ? json([...new Set(options.source_run_ids)]) : null, toolName, startedAt, json(sanitizedInput), sha256(canonicalJson(sanitizedInput)),
    );
    return { run_id: runId, started_at: startedAt };
  }

  async writeArtifact(runId: string, label: string, content: string | Uint8Array, mediaType = "text/plain"): Promise<ArtifactRef> {
    await this.initialize(); this.assertRunExists(runId);
    const bytes = this.sanitizeArtifactContent(content, mediaType);
    const persisted = await this.persistArtifactContent(bytes, mediaType);
    const ref: ArtifactRef = { artifact_id: `artifact-${randomUUID()}`, label, media_type: mediaType, size_bytes: persisted.size_bytes, sha256: persisted.sha256 };
    this.db.prepare("INSERT INTO artifacts(artifact_id, label, sha256, media_type, size_bytes, relative_path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)").run(ref.artifact_id, ref.label, ref.sha256, ref.media_type, ref.size_bytes, persisted.relative_path, now());
    this.db.prepare("INSERT INTO run_artifacts(run_id, artifact_id) VALUES (?, ?)").run(runId, ref.artifact_id);
    return ref;
  }

  private sanitizeArtifactContent(content: string | Uint8Array, mediaType: string): Buffer {
    if (typeof content === "string") return Buffer.from(redactText(content), "utf8");
    const bytes = Buffer.from(content);
    return isTextualMediaType(mediaType) ? Buffer.from(redactText(bytes.toString("utf8")), "utf8") : bytes;
  }

  private async persistArtifactContent(bytes: Uint8Array, mediaType: string): Promise<{ sha256: string; size_bytes: number; relative_path: string }> {
    const digest = sha256(bytes); const extension = mediaType === "application/json" || mediaType.endsWith("+json") ? "json" : "log"; const fileName = `${digest.slice("sha256:".length)}.${extension}`; const target = pathInside(this.artifactRoot, fileName);
    await mkdir(this.artifactRoot, { recursive: true, mode: 0o700 });
    if (!existsSync(target)) {
      const temporary = `${target}.${randomUUID()}.tmp`;
      await writeFile(temporary, bytes, { mode: 0o600 });
      try { await rename(temporary, target); } catch (error: unknown) { if (!existsSync(target)) throw error; }
    }
    return { sha256: digest, size_bytes: bytes.length, relative_path: relative(this.root, target) };
  }

  async finishRun(runId: string, args: FinishRunArgs): Promise<void> {
    await this.initialize(); const prior = this.runRow(runId);
    if (prior.status !== "running") throw new BittuneError("state_conflict", `Run ${runId} is already committed.`, false);
    const sanitizedInput = redact(args.input);
    const observation = redact(args.observation) as Observation;
    const observationHash = sha256(canonicalJson(observation));
    const finishedAt = now();
    this.db.exec("BEGIN IMMEDIATE");
    try {
      this.db.prepare("UPDATE runs SET tool_name = ?, finished_at = ?, status = ?, provenance_type = ?, input_json = ?, input_hash = ?, provider_json = ?, observation_hash = ?, parent_run_id = COALESCE(?, parent_run_id), source_run_ids_json = COALESCE(?, source_run_ids_json) WHERE run_id = ? AND status = 'running'").run(
        args.tool_name, finishedAt, args.status, args.provenance_type, json(sanitizedInput), sha256(canonicalJson(sanitizedInput)), args.provider ? json(args.provider) : null, observationHash, args.parent_run_id ?? null, args.source_run_ids ? json([...new Set(args.source_run_ids)]) : null, runId,
      );
      this.db.prepare("INSERT INTO observations(run_id, summary, observation_json) VALUES (?, ?, ?)").run(runId, observation.summary, json(observation));
      for (const artifact of args.artifacts) this.db.prepare("INSERT OR IGNORE INTO run_artifacts(run_id, artifact_id) VALUES (?, ?)").run(runId, artifact.artifact_id);
      this.db.exec("COMMIT");
    } catch (error) { this.db.exec("ROLLBACK"); throw error; }
  }

  async cancelRun(runId: string, input: unknown, message = "Operation cancelled."): Promise<void> {
    const row = this.runRow(runId); if (row.status !== "running") return;
    const observation: Observation = { ok: false, summary: message, provenance_type: "estimated", measured_at: now(), run_id: runId, warnings: [], artifacts: [], error: { code: "cancelled", message, retryable: true } };
    await this.finishRun(runId, { tool_name: row.tool_name, started_at: row.started_at, status: "cancelled", provenance_type: "estimated", input, artifacts: [], observation });
  }

  async getRun(runId: string): Promise<{ manifest: RunManifest; observation: Observation }> {
    await this.initialize(); const row = this.runRow(runId); const observationRow = this.db.prepare("SELECT observation_json FROM observations WHERE run_id = ?").get(runId) as { observation_json: string } | undefined;
    if (!observationRow || !row.finished_at || !row.observation_hash) throw new BittuneError("run_not_found", `找不到完整的 Run Record ${runId}。`);
    const artifacts = this.runArtifacts(runId);
    const manifest: RunManifest = { schema_version: STATE_SCHEMA_VERSION, run_id: row.run_id, namespace_id: row.namespace_id, session_id: row.session_id, tool_call_id: row.tool_call_id, ...(row.parent_run_id ? { parent_run_id: row.parent_run_id } : {}), ...(row.source_run_ids_json ? { source_run_ids: parse<string[]>(row.source_run_ids_json) } : {}), tool_name: row.tool_name, started_at: row.started_at, finished_at: row.finished_at, status: row.status, provenance_type: row.provenance_type, input_hash: row.input_hash, input: parse(row.input_json), ...(row.provider_json ? { provider: parse(row.provider_json) } : {}), observation_hash: row.observation_hash, artifacts };
    const observation = parse<Observation>(observationRow.observation_json);
    if (sha256(canonicalJson(observation)) !== manifest.observation_hash) throw new BittuneError("state_conflict", `Run ${runId} observation hash mismatch.`, false);
    return { manifest, observation };
  }

  async listRuns(filter: { tool_name?: string; status?: Exclude<RunStatus, "running" | "incomplete">; limit?: number } = {}): Promise<RunManifest[]> {
    await this.initialize();
    const clauses = ["namespace_id = ?", "status != 'running'"]; const params: Array<string | number | null> = [this.namespaceId];
    if (filter.tool_name) { clauses.push("tool_name = ?"); params.push(filter.tool_name); }
    if (filter.status) { clauses.push("status = ?"); params.push(filter.status); }
    const limit = Math.min(filter.limit ?? 50, 100);
    const rows = this.db.prepare(`SELECT * FROM runs WHERE ${clauses.join(" AND ")} ORDER BY finished_at DESC LIMIT ?`).all(...params, limit) as unknown as RunRow[];
    const records: RunManifest[] = [];
    for (const row of rows) {
      if (!row.finished_at || !row.observation_hash) continue;
      records.push({ schema_version: STATE_SCHEMA_VERSION, run_id: row.run_id, namespace_id: row.namespace_id, session_id: row.session_id, tool_call_id: row.tool_call_id, ...(row.parent_run_id ? { parent_run_id: row.parent_run_id } : {}), ...(row.source_run_ids_json ? { source_run_ids: parse<string[]>(row.source_run_ids_json) } : {}), tool_name: row.tool_name, started_at: row.started_at, finished_at: row.finished_at, status: row.status, provenance_type: row.provenance_type, input_hash: row.input_hash, input: parse(row.input_json), ...(row.provider_json ? { provider: parse(row.provider_json) } : {}), observation_hash: row.observation_hash, artifacts: this.runArtifacts(row.run_id) });
    }
    return records;
  }

  async readArtifact(runId: string, artifactId: string, offsetBytes: number, maxBytes: number): Promise<{ text: string; total_bytes: number; truncated: boolean }> {
    await this.initialize();
    if (!/^run-[a-f0-9-]{36}$/.test(runId)) throw new BittuneError("invalid_input", "run_id is invalid.", false);
    if (!/^artifact-[a-f0-9-]{36}$/.test(artifactId)) throw new BittuneError("invalid_input", "artifact_id is invalid.", false);
    if (!Number.isInteger(offsetBytes) || offsetBytes < 0 || !Number.isInteger(maxBytes) || maxBytes < 1 || maxBytes > 65_536) throw new BittuneError("invalid_input", "Artifact 读取范围不合法。", false);
    const row = this.db.prepare("SELECT artifacts.sha256, artifacts.relative_path, artifacts.size_bytes FROM artifacts JOIN run_artifacts ON run_artifacts.artifact_id = artifacts.artifact_id JOIN runs ON runs.run_id = run_artifacts.run_id WHERE run_artifacts.run_id = ? AND artifacts.artifact_id = ? AND runs.namespace_id = ?").get(runId, artifactId, this.namespaceId) as { sha256: string; relative_path: string; size_bytes: number } | undefined;
    if (!row) throw new BittuneError("artifact_not_found", `Run ${runId} 中没有 Artifact ${artifactId}。`);
    const path = pathInside(this.root, row.relative_path); const bytes = await readFile(path);
    if (sha256(bytes) !== row.sha256) throw new BittuneError("state_conflict", `Artifact ${artifactId} hash mismatch.`, false);
    return { text: bytes.subarray(offsetBytes, offsetBytes + maxBytes).toString("utf8"), total_bytes: bytes.length, truncated: offsetBytes + maxBytes < bytes.length };
  }

  private createSchema(database: DatabaseSync): void {
    database.exec(`
      CREATE TABLE IF NOT EXISTS namespaces (namespace_id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY, namespace_id TEXT NOT NULL REFERENCES namespaces(namespace_id), session_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
        parent_run_id TEXT, source_run_ids_json TEXT, tool_name TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
        status TEXT NOT NULL CHECK(status IN ('running','completed','failed','cancelled','incomplete')), provenance_type TEXT NOT NULL,
        input_json TEXT NOT NULL, input_hash TEXT NOT NULL, provider_json TEXT, observation_hash TEXT
      );
      CREATE INDEX IF NOT EXISTS idx_runs_namespace_finished ON runs(namespace_id, finished_at DESC);
      CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(namespace_id, session_id, started_at DESC);
      CREATE TABLE IF NOT EXISTS observations (run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE, summary TEXT NOT NULL, observation_json TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS artifacts (artifact_id TEXT PRIMARY KEY, label TEXT NOT NULL, sha256 TEXT NOT NULL, media_type TEXT NOT NULL, size_bytes INTEGER NOT NULL, relative_path TEXT NOT NULL, created_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS run_artifacts (run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE, artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id), PRIMARY KEY(run_id, artifact_id));
      CREATE TABLE IF NOT EXISTS deployment_presets (namespace_id TEXT NOT NULL REFERENCES namespaces(namespace_id), preset_id TEXT NOT NULL, version TEXT NOT NULL, version_number INTEGER NOT NULL, record_json TEXT NOT NULL, PRIMARY KEY(namespace_id, preset_id, version));
      CREATE TABLE IF NOT EXISTS capacity_baselines (namespace_id TEXT NOT NULL REFERENCES namespaces(namespace_id), baseline_id TEXT NOT NULL, version TEXT NOT NULL, version_number INTEGER NOT NULL, record_json TEXT NOT NULL, PRIMARY KEY(namespace_id, baseline_id, version));
      CREATE TABLE IF NOT EXISTS services (namespace_id TEXT NOT NULL REFERENCES namespaces(namespace_id), instance_id TEXT NOT NULL, record_json TEXT NOT NULL, PRIMARY KEY(namespace_id, instance_id));
      CREATE TABLE IF NOT EXISTS experiment_specs (namespace_id TEXT NOT NULL REFERENCES namespaces(namespace_id), experiment_id TEXT NOT NULL, version TEXT NOT NULL, version_number INTEGER NOT NULL, record_json TEXT NOT NULL, PRIMARY KEY(namespace_id, experiment_id, version));
      CREATE TABLE IF NOT EXISTS experiment_trials (namespace_id TEXT NOT NULL REFERENCES namespaces(namespace_id), experiment_id TEXT NOT NULL, trial_id TEXT NOT NULL, record_json TEXT NOT NULL, PRIMARY KEY(namespace_id, experiment_id, trial_id));
      CREATE TABLE IF NOT EXISTS experiment_comparisons (namespace_id TEXT NOT NULL REFERENCES namespaces(namespace_id), experiment_id TEXT NOT NULL, comparison_id TEXT NOT NULL, record_json TEXT NOT NULL, PRIMARY KEY(namespace_id, experiment_id, comparison_id));
      CREATE TABLE IF NOT EXISTS tool_invocation_audit (id TEXT PRIMARY KEY, namespace_id TEXT NOT NULL REFERENCES namespaces(namespace_id), session_id TEXT, tool_call_id TEXT, tool_name TEXT NOT NULL, occurred_at TEXT NOT NULL, detail_json TEXT NOT NULL);
    `);
  }

  private records<T>(sql: string, ...params: Array<string | number | null>): T[] { return (this.db.prepare(sql).all(...params) as unknown as JsonRow[]).map((row) => { const record = parse<T & { schema_version?: number }>(row.record_json); this.ensureSchema(record); return record; }); }
  private ensureSchema(record: { schema_version?: number }): void { if (record.schema_version !== STATE_SCHEMA_VERSION) throw new BittuneError("state_schema_mismatch", "State record uses an unsupported schema; initialize a new State Store.", false); }
  private runRow(runId: string): RunRow { if (!/^run-[a-f0-9-]{36}$/.test(runId)) throw new BittuneError("invalid_input", "run_id is invalid.", false); const row = this.db.prepare("SELECT * FROM runs WHERE run_id = ? AND namespace_id = ?").get(runId, this.namespaceId) as RunRow | undefined; if (!row) throw new BittuneError("run_not_found", `找不到 Run Record ${runId}。`); return row; }
  private assertRunExists(runId: string): void { this.runRow(runId); }
  private runArtifacts(runId: string): ArtifactRef[] { return this.db.prepare("SELECT artifacts.artifact_id, artifacts.media_type, artifacts.size_bytes, artifacts.sha256, artifacts.label FROM artifacts JOIN run_artifacts ON run_artifacts.artifact_id = artifacts.artifact_id WHERE run_artifacts.run_id = ? ORDER BY artifacts.created_at").all(runId) as unknown as ArtifactRef[]; }
  private versionNumber(value: string): number { return Number(value.slice(1)); }
  private async versionedWrite<T>(kind: "deployment-presets" | "capacity-baselines" | "experiment-specs", id: string, operation: (version: string) => Promise<T>, expectedParent?: string): Promise<T> {
    const table = kind.replaceAll("-", "_"); const idColumn = kind === "experiment-specs" ? "experiment_id" : kind === "deployment-presets" ? "preset_id" : "baseline_id";
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const parent = this.db.prepare(`SELECT version FROM ${table} WHERE namespace_id = ? AND ${idColumn} = ? ORDER BY version_number DESC LIMIT 1`).get(this.namespaceId, id) as { version: string } | undefined;
      if (expectedParent !== undefined && parent?.version !== expectedParent) throw new BittuneError("state_conflict", `Expected parent version ${expectedParent}, current version is ${parent?.version ?? "none"}.`, true);
      const result = await operation(`v${(parent ? this.versionNumber(parent.version) : 0) + 1}`);
      this.db.exec("COMMIT"); return result;
    } catch (error) { this.db.exec("ROLLBACK"); throw error; }
  }
  private normalizeService(manifest: ServiceManifest): ServiceManifest { return { ...manifest, observation: manifest.observation ?? defaultStateObservation(manifest.last_known_status) }; }

  async resolveModelSnapshot(preset: DeploymentPreset): Promise<{ cache_root: string; host_snapshot_path: string } | undefined> { for (const root of this.hfCacheRoots) { const path = this.snapshotPathAt(root, preset); if (await this.snapshotExistsAt(path)) return { cache_root: root, host_snapshot_path: path }; } return undefined; }
  async modelSnapshotPath(preset: DeploymentPreset): Promise<string> { return (await this.resolveModelSnapshot(preset))?.host_snapshot_path ?? this.snapshotPathAt(this.hfCacheRoot, preset); }
  async snapshotExists(preset: DeploymentPreset): Promise<boolean> { return (await this.resolveModelSnapshot(preset)) !== undefined; }
  private snapshotPathAt(root: string, preset: DeploymentPreset): string { return join(root, "hub", `models--${preset.model_id.replace("/", "--")}`, "snapshots", preset.model_revision); }
  private async snapshotExistsAt(path: string): Promise<boolean> { try { if (!(await stat(path)).isDirectory()) return false; const entries = await readdir(path); return (await Promise.all(entries.filter((name) => /\.(safetensors|bin|pt|pth|gguf)$/i.test(name)).map(async (name) => (await stat(join(path, name))).isFile()))).some(Boolean); } catch (error: unknown) { if ((error as NodeJS.ErrnoException).code === "ENOENT") return false; throw error; } }

  }
