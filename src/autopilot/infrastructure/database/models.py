"""PostgreSQL ORM entities for M3 state, queue, artifacts, events, and audit."""

from __future__ import annotations

from datetime import datetime
from typing import Final

from pydantic import JsonValue
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from autopilot.infrastructure.database.base import APP_SCHEMA, Base

ID_LENGTH: Final = 64
DIGEST_LENGTH: Final = 71


class ExperimentRow(Base):
    __tablename__ = "experiments"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    requirements_json: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    hardware_passport_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    workload_spec_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    slo_spec_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    champion_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    graph_state_json: Mapped[dict[str, JsonValue]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('active','waiting_input','waiting_approval','completed',"
            "'failed','cancelled')",
            name="experiment_status",
        ),
        CheckConstraint(
            "phase IN ('requirements','environment','planning','approval','deployment','benchmark',"
            "'optimization','verification','report','completed','failed','cancelled')",
            name="experiment_phase",
        ),
    )


class PlanRow(Base):
    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey(f"{APP_SCHEMA}.experiments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    body_json: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(DIGEST_LENGTH), nullable=False, unique=True)
    risk_level: Mapped[str] = mapped_column(String(2), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "kind IN ('environment','capacity','deployment','benchmark','optimization',"
            "'verification','champion','evidence')",
            name="plan_kind",
        ),
        CheckConstraint("risk_level IN ('L0','L1','L2','L3')", name="plan_risk_level"),
        CheckConstraint(
            "status IN ('draft','approved','rejected','executed')",
            name="plan_status",
        ),
        UniqueConstraint("experiment_id", "id", name="uq_plans_experiment_id"),
        UniqueConstraint(
            "experiment_id",
            "id",
            "plan_hash",
            name="uq_plans_experiment_id_plan_hash",
        ),
        Index("ix_plans_experiment_created", "experiment_id", "created_at"),
    )


class ApprovalRow(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    plan_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(DIGEST_LENGTH), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(2), nullable=False)
    requester_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    requester_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    requester_role: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    decided_by_kind: Mapped[str | None] = mapped_column(String(16))
    decided_by_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    decided_by_role: Mapped[str | None] = mapped_column(String(16))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str | None] = mapped_column(String(4096))

    __table_args__ = (
        CheckConstraint(
            "schema_version = 'approval/v2'",
            name="approval_schema_version",
        ),
        CheckConstraint("risk_level = 'L2'", name="approval_l2_only"),
        CheckConstraint(
            "requester_kind = 'human' AND requester_role IN ('operator','admin')",
            name="approval_human_requester",
        ),
        CheckConstraint(
            "decision IN ('pending','approved','rejected','expired')",
            name="approval_decision",
        ),
        CheckConstraint("requested_at < expires_at", name="approval_expiry"),
        CheckConstraint(
            "((decision IN ('pending','expired') AND decided_by_kind IS NULL "
            "AND decided_by_id IS NULL AND decided_by_role IS NULL AND decided_at IS NULL) OR "
            "(decision IN ('approved','rejected') AND decided_by_kind = 'human' "
            "AND decided_by_id IS NOT NULL AND decided_by_role = 'admin' "
            "AND decided_at IS NOT NULL AND requested_at <= decided_at "
            "AND decided_at < expires_at))",
            name="approval_decision_metadata",
        ),
        CheckConstraint(
            "decided_by_id IS NULL OR requester_id <> decided_by_id",
            name="approval_no_self_decision",
        ),
        CheckConstraint(
            "comment IS NULL OR length(btrim(comment)) BETWEEN 1 AND 4096",
            name="approval_comment",
        ),
        ForeignKeyConstraint(
            ["experiment_id", "plan_id", "plan_hash"],
            [
                f"{APP_SCHEMA}.plans.experiment_id",
                f"{APP_SCHEMA}.plans.id",
                f"{APP_SCHEMA}.plans.plan_hash",
            ],
            name="fk_approvals_plan_material",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "experiment_id",
            "plan_id",
            "plan_hash",
            "action",
            name="uq_approvals_plan_hash_action",
        ),
        UniqueConstraint("experiment_id", "id", name="uq_approvals_experiment_id"),
        Index("ix_approvals_plan_action", "experiment_id", "plan_id", "action"),
    )


class ToolSetSnapshotRow(Base):
    __tablename__ = "toolset_snapshots"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_set_version: Mapped[str] = mapped_column(String(DIGEST_LENGTH), nullable=False)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey(f"{APP_SCHEMA}.experiments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    subject_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    subject_role: Mapped[str | None] = mapped_column(String(16))
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    hardware_capabilities_json: Mapped[list[JsonValue]] = mapped_column(JSONB, nullable=False)
    enabled_providers_json: Mapped[list[JsonValue]] = mapped_column(JSONB, nullable=False)
    enabled_feature_flags_json: Mapped[list[JsonValue]] = mapped_column(JSONB, nullable=False)
    tools_json: Mapped[list[JsonValue]] = mapped_column(JSONB, nullable=False)
    policy_decision_ids_json: Mapped[list[JsonValue]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "schema_version = 'tool-set-snapshot/v1'",
            name="toolset_snapshot_schema_version",
        ),
        CheckConstraint(
            "((subject_kind = 'human' AND subject_role IN ('viewer','operator','admin')) OR "
            "(subject_kind = 'service' AND subject_role IS NULL))",
            name="toolset_snapshot_subject",
        ),
        CheckConstraint(
            "phase IN ('requirements','environment','planning','approval','deployment',"
            "'benchmark','optimization','verification','report','completed','failed','cancelled')",
            name="toolset_snapshot_phase",
        ),
        CheckConstraint(
            "jsonb_typeof(hardware_capabilities_json) = 'array' "
            "AND jsonb_typeof(enabled_providers_json) = 'array' "
            "AND jsonb_typeof(enabled_feature_flags_json) = 'array' "
            "AND jsonb_typeof(tools_json) = 'array' "
            "AND jsonb_typeof(policy_decision_ids_json) = 'array'",
            name="toolset_snapshot_json_arrays",
        ),
        UniqueConstraint(
            "experiment_id",
            "id",
            name="uq_toolset_snapshots_experiment_id",
        ),
        Index(
            "ix_toolset_snapshots_experiment_created",
            "experiment_id",
            "created_at",
        ),
    )


class ArtifactRow(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey(f"{APP_SCHEMA}.experiments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    content_type: Mapped[str] = mapped_column(String(256), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(DIGEST_LENGTH), nullable=False)
    producer_component: Mapped[str] = mapped_column(String(256), nullable=False)
    producer_version: Mapped[str] = mapped_column(String(256), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "schema_version = 'artifact-metadata/v1'",
            name="artifact_schema_version",
        ),
        CheckConstraint("size_bytes >= 0", name="artifact_non_negative_size"),
        UniqueConstraint("experiment_id", "id", name="uq_artifacts_experiment_id"),
        Index("ix_artifacts_experiment_category", "experiment_id", "category"),
    )


class DeploymentRow(Base):
    """Read model for the deployment API; lifecycle is owned by the Runner adapter."""

    __tablename__ = "deployments"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey(f"{APP_SCHEMA}.experiments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    candidate_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    container_id: Mapped[str | None] = mapped_column(String(256))
    endpoint: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    parameter_hash: Mapped[str] = mapped_column(String(DIGEST_LENGTH), nullable=False)
    image_digest: Mapped[str] = mapped_column(String(256), nullable=False)
    model_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    gpu_id: Mapped[int] = mapped_column(Integer, nullable=False)
    logs_artifact_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "schema_version = 'deployment-record/v1'",
            name="deployment_schema_version",
        ),
        CheckConstraint("gpu_id = 0", name="deployment_single_gpu_zero"),
        CheckConstraint(
            "status IN ('starting','healthy','stopping','stopped','failed')",
            name="deployment_status",
        ),
        UniqueConstraint("experiment_id", "id", name="uq_deployments_experiment_id"),
        Index("ix_deployments_experiment_updated", "experiment_id", "updated_at"),
    )


class JobRow(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey(f"{APP_SCHEMA}.experiments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    plan_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_job_id: Mapped[str | None] = mapped_column(String(256))
    progress_json: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    error_json: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    result_artifact_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_schema_version: Mapped[str | None] = mapped_column(String(64))
    lease_owner: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    lease_acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    __table_args__ = (
        CheckConstraint("schema_version = 'job/v1'", name="job_schema_version"),
        CheckConstraint(
            "kind IN ('environment','deployment','benchmark','optimization',"
            "'verification','evidence')",
            name="job_kind",
        ),
        CheckConstraint(
            "status IN ('queued','validating','waiting_approval','running','succeeded','failed',"
            "'cancelled','timed_out')",
            name="job_status",
        ),
        CheckConstraint(
            "((lease_owner IS NULL AND lease_schema_version IS NULL "
            "AND lease_acquired_at IS NULL AND lease_heartbeat_at IS NULL "
            "AND lease_expires_at IS NULL AND lease_generation >= 0) OR "
            "(lease_owner IS NOT NULL AND lease_schema_version = 'job-lease/v1' "
            "AND lease_acquired_at IS NOT NULL "
            "AND lease_heartbeat_at IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND lease_generation >= 1 "
            "AND lease_acquired_at <= lease_heartbeat_at "
            "AND lease_heartbeat_at < lease_expires_at))",
            name="job_lease_consistency",
        ),
        CheckConstraint(
            "(status NOT IN ('validating','waiting_approval','running') OR "
            "lease_owner IS NOT NULL) AND "
            "(status NOT IN ('succeeded','failed','cancelled','timed_out') OR "
            "lease_owner IS NULL)",
            name="job_lease_by_status",
        ),
        CheckConstraint(
            "((status IN ('queued','validating','waiting_approval') AND started_at IS NULL) OR "
            "(status NOT IN ('queued','validating','waiting_approval'))) AND "
            "((status IN ('running','succeeded') AND started_at IS NOT NULL) OR "
            "(status NOT IN ('running','succeeded'))) AND "
            "((status IN ('succeeded','failed','cancelled','timed_out') "
            "AND ended_at IS NOT NULL) OR "
            "(status NOT IN ('succeeded','failed','cancelled','timed_out') AND ended_at IS NULL))",
            name="job_timestamps_by_status",
        ),
        CheckConstraint(
            "(started_at IS NULL OR started_at >= submitted_at) AND "
            "(ended_at IS NULL OR ended_at >= COALESCE(started_at, submitted_at)) AND "
            "(cancel_requested_at IS NULL OR cancel_requested_at >= submitted_at) AND "
            "(cancel_requested_at IS NULL OR ended_at IS NULL OR cancel_requested_at <= ended_at)",
            name="job_chronology",
        ),
        CheckConstraint(
            "((status = 'succeeded' AND result_artifact_id IS NOT NULL AND error_json IS NULL) OR "
            "(status <> 'succeeded' AND result_artifact_id IS NULL)) AND "
            "((status IN ('failed','timed_out') AND error_json IS NOT NULL) OR "
            "(status NOT IN ('failed','timed_out') AND error_json IS NULL))",
            name="job_terminal_data",
        ),
        ForeignKeyConstraint(
            ["experiment_id", "plan_id"],
            [f"{APP_SCHEMA}.plans.experiment_id", f"{APP_SCHEMA}.plans.id"],
            name="fk_jobs_experiment_plan",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["experiment_id", "result_artifact_id"],
            [f"{APP_SCHEMA}.artifacts.experiment_id", f"{APP_SCHEMA}.artifacts.id"],
            name="fk_jobs_experiment_result_artifact",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("experiment_id", "id", name="uq_jobs_experiment_id"),
        CheckConstraint("version >= 1", name="job_positive_version"),
        Index("ix_jobs_experiment_submitted", "experiment_id", "submitted_at"),
        Index(
            "ix_jobs_claimable",
            "status",
            "lease_expires_at",
            "submitted_at",
            postgresql_where=text("status IN ('queued','validating','waiting_approval','running')"),
        ),
    )


class OptimizationTrialRow(Base):
    __tablename__ = "optimization_trials"

    trial_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    trial_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    plan_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(DIGEST_LENGTH), nullable=False)
    study_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    trial_number: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    benchmark_run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    parameters_json: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    constraints_json: Mapped[list[JsonValue]] = mapped_column(JSONB, nullable=False)
    objective_json: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    provenance_json: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    evidence_json: Mapped[list[JsonValue]] = mapped_column(JSONB, nullable=False)
    error_json: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    reservation_json: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, nullable=False)
    checkpoint_stage: Mapped[str | None] = mapped_column(String(32))
    provider_resource_id: Mapped[str | None] = mapped_column(String(256))
    evidence_run_json: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    __table_args__ = (
        CheckConstraint(
            "schema_version = 'optimization-trial-entry/v1'",
            name="optimization_trial_schema_version",
        ),
        CheckConstraint(
            "trial_schema_version = 'optimization-trial/v1'",
            name="optimization_trial_record_schema_version",
        ),
        CheckConstraint(
            "status IN ('suggested','rejected_static','deployment_failed','benchmark_failed',"
            "'oom','constraint_failed','completed','cancelled')",
            name="optimization_trial_status",
        ),
        CheckConstraint(
            "jsonb_typeof(parameters_json) = 'object' "
            "AND jsonb_typeof(constraints_json) = 'array' "
            "AND jsonb_typeof(evidence_json) = 'array' "
            "AND jsonb_typeof(reservation_json) = 'object'",
            name="optimization_trial_json_shapes",
        ),
        CheckConstraint(
            "((checkpoint_stage IS NULL AND provider_resource_id IS NULL) OR "
            "(status = 'suggested' AND checkpoint_stage IN ('deployment','benchmark') "
            "AND provider_resource_id IS NOT NULL))",
            name="optimization_trial_checkpoint",
        ),
        CheckConstraint(
            "((status = 'suggested' AND evidence_run_json IS NULL AND ended_at IS NULL) OR "
            "(status <> 'suggested' AND evidence_run_json IS NOT NULL AND ended_at IS NOT NULL))",
            name="optimization_trial_terminal_evidence",
        ),
        CheckConstraint(
            "((status IN ('completed','constraint_failed') AND objective_json IS NOT NULL "
            "AND provenance_json IS NOT NULL AND jsonb_array_length(constraints_json) > 0) OR "
            "(status NOT IN ('completed','constraint_failed') AND objective_json IS NULL "
            "AND provenance_json IS NULL AND jsonb_array_length(constraints_json) = 0))",
            name="optimization_trial_measured_data",
        ),
        CheckConstraint(
            "((status IN ('rejected_static','deployment_failed','benchmark_failed','oom') "
            "AND error_json IS NOT NULL) OR "
            "(status NOT IN ('rejected_static','deployment_failed','benchmark_failed','oom') "
            "AND error_json IS NULL))",
            name="optimization_trial_error",
        ),
        CheckConstraint(
            "created_at <= updated_at AND (ended_at IS NULL OR updated_at <= ended_at)",
            name="optimization_trial_chronology",
        ),
        CheckConstraint("version >= 1", name="optimization_trial_positive_version"),
        ForeignKeyConstraint(
            ["experiment_id", "plan_id", "plan_hash"],
            [
                f"{APP_SCHEMA}.plans.experiment_id",
                f"{APP_SCHEMA}.plans.id",
                f"{APP_SCHEMA}.plans.plan_hash",
            ],
            name="fk_optimization_trials_plan_material",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "experiment_id",
            "study_id",
            "trial_number",
            name="uq_optimization_trials_study_number",
        ),
        UniqueConstraint(
            "experiment_id",
            "trial_id",
            name="uq_optimization_trials_experiment_id",
        ),
        Index(
            "ix_optimization_trials_study_status",
            "experiment_id",
            "study_id",
            "status",
            "trial_number",
        ),
    )


class IdempotencyRow(Base):
    __tablename__ = "idempotency_records"

    idempotency_key: Mapped[str] = mapped_column(String(DIGEST_LENGTH), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(DIGEST_LENGTH), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey(f"{APP_SCHEMA}.experiments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    job_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "schema_version = 'idempotency-record/v1'",
            name="idempotency_schema_version",
        ),
        ForeignKeyConstraint(
            ["experiment_id", "job_id"],
            [f"{APP_SCHEMA}.jobs.experiment_id", f"{APP_SCHEMA}.jobs.id"],
            name="fk_idempotency_records_experiment_job",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint(
            "idempotency_key",
            "experiment_id",
            "job_id",
            "request_hash",
            "action",
            name="uq_idempotency_authorization_material",
        ),
    )


class JobAuthorizationRow(Base):
    __tablename__ = "job_authorizations"

    job_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    subject_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    subject_role: Mapped[str | None] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(2), nullable=False)
    plan_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(DIGEST_LENGTH), nullable=False)
    approval_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    tool_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_set_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    tool_set_version: Mapped[str] = mapped_column(String(DIGEST_LENGTH), nullable=False)
    policy_decision_id: Mapped[str] = mapped_column(String(256), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(DIGEST_LENGTH), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(DIGEST_LENGTH), nullable=False)
    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "schema_version = 'job-authorization/v1'",
            name="job_authorization_schema_version",
        ),
        CheckConstraint(
            "risk_level IN ('L0','L1','L2')",
            name="job_authorization_risk_level",
        ),
        CheckConstraint(
            "((risk_level = 'L2' AND approval_id IS NOT NULL) OR "
            "(risk_level IN ('L0','L1') AND approval_id IS NULL))",
            name="job_authorization_approval",
        ),
        CheckConstraint(
            "((subject_kind = 'human' AND subject_role IN ('viewer','operator','admin')) OR "
            "(subject_kind = 'service' AND subject_role IS NULL))",
            name="job_authorization_subject",
        ),
        ForeignKeyConstraint(
            ["experiment_id", "job_id"],
            [f"{APP_SCHEMA}.jobs.experiment_id", f"{APP_SCHEMA}.jobs.id"],
            name="fk_job_authorizations_job",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["experiment_id", "plan_id", "plan_hash"],
            [
                f"{APP_SCHEMA}.plans.experiment_id",
                f"{APP_SCHEMA}.plans.id",
                f"{APP_SCHEMA}.plans.plan_hash",
            ],
            name="fk_job_authorizations_plan_material",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["experiment_id", "approval_id"],
            [f"{APP_SCHEMA}.approvals.experiment_id", f"{APP_SCHEMA}.approvals.id"],
            name="fk_job_authorizations_approval",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["experiment_id", "tool_set_id"],
            [
                f"{APP_SCHEMA}.toolset_snapshots.experiment_id",
                f"{APP_SCHEMA}.toolset_snapshots.id",
            ],
            name="fk_job_authorizations_toolset",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["idempotency_key", "experiment_id", "job_id", "request_hash", "action"],
            [
                f"{APP_SCHEMA}.idempotency_records.idempotency_key",
                f"{APP_SCHEMA}.idempotency_records.experiment_id",
                f"{APP_SCHEMA}.idempotency_records.job_id",
                f"{APP_SCHEMA}.idempotency_records.request_hash",
                f"{APP_SCHEMA}.idempotency_records.action",
            ],
            name="fk_job_authorizations_idempotency_material",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint(
            "experiment_id",
            "job_id",
            name="uq_job_authorizations_experiment_id",
        ),
        Index(
            "ix_job_authorizations_plan_action",
            "experiment_id",
            "plan_id",
            "action",
        ),
    )


class EventRow(Base):
    __tablename__ = "events"

    sequence: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, unique=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey(f"{APP_SCHEMA}.experiments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    job_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(32))
    current_status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[dict[str, JsonValue]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )

    __table_args__ = (
        CheckConstraint("schema_version = 'job-event/v1'", name="event_schema_version"),
        CheckConstraint(
            "current_status IN ('queued','validating','waiting_approval','running','succeeded',"
            "'failed','cancelled','timed_out')",
            name="event_current_status",
        ),
        CheckConstraint(
            "previous_status IS NULL OR previous_status IN "
            "('queued','validating','waiting_approval',"
            "'running','succeeded','failed','cancelled','timed_out')",
            name="event_previous_status",
        ),
        ForeignKeyConstraint(
            ["experiment_id", "job_id"],
            [f"{APP_SCHEMA}.jobs.experiment_id", f"{APP_SCHEMA}.jobs.id"],
            name="fk_events_experiment_job",
            ondelete="RESTRICT",
        ),
        Index("ix_events_job_sequence", "job_id", "sequence"),
        Index("ix_events_experiment_sequence", "experiment_id", "sequence"),
    )


class AuditEventRow(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str | None] = mapped_column(
        ForeignKey(f"{APP_SCHEMA}.experiments.id", ondelete="RESTRICT")
    )
    job_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    actor: Mapped[str] = mapped_column(String(256), nullable=False)
    action: Mapped[str] = mapped_column(String(256), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(256), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(256), nullable=False)
    request_id: Mapped[str] = mapped_column(String(256), nullable=False)
    decision_id: Mapped[str | None] = mapped_column(String(256))
    before_artifact_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    after_artifact_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("schema_version = 'audit-event/v1'", name="audit_schema_version"),
        CheckConstraint("result IN ('succeeded','failed','denied')", name="audit_result"),
        CheckConstraint(
            "experiment_id IS NOT NULL OR (job_id IS NULL AND before_artifact_id IS NULL "
            "AND after_artifact_id IS NULL)",
            name="audit_experiment_binding",
        ),
        ForeignKeyConstraint(
            ["experiment_id", "job_id"],
            [f"{APP_SCHEMA}.jobs.experiment_id", f"{APP_SCHEMA}.jobs.id"],
            name="fk_audit_events_experiment_job",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["experiment_id", "before_artifact_id"],
            [f"{APP_SCHEMA}.artifacts.experiment_id", f"{APP_SCHEMA}.artifacts.id"],
            name="fk_audit_events_experiment_before_artifact",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["experiment_id", "after_artifact_id"],
            [f"{APP_SCHEMA}.artifacts.experiment_id", f"{APP_SCHEMA}.artifacts.id"],
            name="fk_audit_events_experiment_after_artifact",
            ondelete="RESTRICT",
        ),
        Index("ix_audit_events_experiment_occurred", "experiment_id", "occurred_at"),
        Index("ix_audit_events_request", "request_id"),
    )
