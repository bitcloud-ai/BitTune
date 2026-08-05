"""Create the initial application persistence schema.

Revision ID: 20260805_0001
Revises: None
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260805_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_SCHEMA = "app"


def upgrade() -> None:
    op.execute(sa.schema.CreateSchema(APP_SCHEMA))
    op.create_table(
        "experiments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("requirements_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("hardware_passport_id", sa.String(length=64), nullable=True),
        sa.Column("workload_spec_id", sa.String(length=64), nullable=True),
        sa.Column("slo_spec_id", sa.String(length=64), nullable=True),
        sa.Column("champion_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active','waiting_input','waiting_approval','completed','failed','cancelled')",
            name="experiment_status",
        ),
        sa.CheckConstraint(
            "phase IN ('requirements','environment','planning','approval','deployment','benchmark',"
            "'optimization','verification','report','completed','failed','cancelled')",
            name="experiment_phase",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_experiments"),
        schema=APP_SCHEMA,
    )
    op.create_table(
        "plans",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("body_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("plan_hash", sa.String(length=71), nullable=False),
        sa.Column("risk_level", sa.String(length=2), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("approved_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('environment','capacity','deployment','benchmark','optimization',"
            "'verification','champion','evidence')",
            name="plan_kind",
        ),
        sa.CheckConstraint(
            "risk_level IN ('L0','L1','L2','L3')",
            name="plan_risk_level",
        ),
        sa.CheckConstraint(
            "status IN ('draft','approved','rejected','executed')",
            name="plan_status",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["app.experiments.id"],
            name="fk_plans_experiment_id_experiments",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_plans"),
        sa.UniqueConstraint("experiment_id", "id", name="uq_plans_experiment_id"),
        sa.UniqueConstraint("plan_hash", name="uq_plans_plan_hash"),
        schema=APP_SCHEMA,
    )
    op.create_index(
        "ix_plans_experiment_created",
        "plans",
        ["experiment_id", "created_at"],
        schema=APP_SCHEMA,
    )
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("content_type", sa.String(length=256), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=71), nullable=False),
        sa.Column("producer_component", sa.String(length=256), nullable=False),
        sa.Column("producer_version", sa.String(length=256), nullable=False),
        sa.Column("storage_path", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = 'artifact-metadata/v1'",
            name="artifact_schema_version",
        ),
        sa.CheckConstraint("size_bytes >= 0", name="artifact_non_negative_size"),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["app.experiments.id"],
            name="fk_artifacts_experiment_id_experiments",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_artifacts"),
        sa.UniqueConstraint("experiment_id", "id", name="uq_artifacts_experiment_id"),
        sa.UniqueConstraint("storage_path", name="uq_artifacts_storage_path"),
        schema=APP_SCHEMA,
    )
    op.create_index(
        "ix_artifacts_experiment_category",
        "artifacts",
        ["experiment_id", "category"],
        schema=APP_SCHEMA,
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider_job_id", sa.String(length=256), nullable=True),
        sa.Column("progress_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result_artifact_id", sa.String(length=64), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_schema_version", sa.String(length=64), nullable=True),
        sa.Column("lease_owner", sa.String(length=64), nullable=True),
        sa.Column("lease_acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_generation", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint("schema_version = 'job/v1'", name="job_schema_version"),
        sa.CheckConstraint(
            "kind IN ('environment','deployment','benchmark','optimization','verification','evidence')",
            name="job_kind",
        ),
        sa.CheckConstraint(
            "status IN ('queued','validating','waiting_approval','running','succeeded','failed',"
            "'cancelled','timed_out')",
            name="job_status",
        ),
        sa.CheckConstraint(
            "((lease_owner IS NULL AND lease_schema_version IS NULL "
            "AND lease_acquired_at IS NULL AND lease_heartbeat_at IS NULL "
            "AND lease_expires_at IS NULL AND lease_generation >= 0) OR "
            "(lease_owner IS NOT NULL AND lease_schema_version = 'job-lease/v1' "
            "AND lease_acquired_at IS NOT NULL "
            "AND lease_heartbeat_at IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND lease_generation >= 1 "
            "AND lease_acquired_at <= lease_heartbeat_at AND lease_heartbeat_at < lease_expires_at))",
            name="job_lease_consistency",
        ),
        sa.CheckConstraint(
            "(status NOT IN ('validating','waiting_approval','running') OR "
            "lease_owner IS NOT NULL) AND "
            "(status NOT IN ('succeeded','failed','cancelled','timed_out') OR "
            "lease_owner IS NULL)",
            name="job_lease_by_status",
        ),
        sa.CheckConstraint(
            "((status IN ('queued','validating','waiting_approval') AND started_at IS NULL) OR "
            "(status NOT IN ('queued','validating','waiting_approval'))) AND "
            "((status IN ('running','succeeded') AND started_at IS NOT NULL) OR "
            "(status NOT IN ('running','succeeded'))) AND "
            "((status IN ('succeeded','failed','cancelled','timed_out') AND ended_at IS NOT NULL) OR "
            "(status NOT IN ('succeeded','failed','cancelled','timed_out') AND ended_at IS NULL))",
            name="job_timestamps_by_status",
        ),
        sa.CheckConstraint(
            "(started_at IS NULL OR started_at >= submitted_at) AND "
            "(ended_at IS NULL OR ended_at >= COALESCE(started_at, submitted_at)) AND "
            "(cancel_requested_at IS NULL OR cancel_requested_at >= submitted_at) AND "
            "(cancel_requested_at IS NULL OR ended_at IS NULL OR cancel_requested_at <= ended_at)",
            name="job_chronology",
        ),
        sa.CheckConstraint(
            "((status = 'succeeded' AND result_artifact_id IS NOT NULL AND error_json IS NULL) OR "
            "(status <> 'succeeded' AND result_artifact_id IS NULL)) AND "
            "((status IN ('failed','timed_out') AND error_json IS NOT NULL) OR "
            "(status NOT IN ('failed','timed_out') AND error_json IS NULL))",
            name="job_terminal_data",
        ),
        sa.CheckConstraint("version >= 1", name="job_positive_version"),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["app.experiments.id"],
            name="fk_jobs_experiment_id_experiments",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id", "plan_id"],
            ["app.plans.experiment_id", "app.plans.id"],
            name="fk_jobs_experiment_plan",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id", "result_artifact_id"],
            ["app.artifacts.experiment_id", "app.artifacts.id"],
            name="fk_jobs_experiment_result_artifact",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
        sa.UniqueConstraint("experiment_id", "id", name="uq_jobs_experiment_id"),
        schema=APP_SCHEMA,
    )
    op.create_index(
        "ix_jobs_experiment_submitted",
        "jobs",
        ["experiment_id", "submitted_at"],
        schema=APP_SCHEMA,
    )
    op.create_index(
        "ix_jobs_claimable",
        "jobs",
        ["status", "lease_expires_at", "submitted_at"],
        unique=False,
        schema=APP_SCHEMA,
        postgresql_where=sa.text(
            "status IN ('queued','validating','waiting_approval','running')"
        ),
    )
    op.create_table(
        "idempotency_records",
        sa.Column("idempotency_key", sa.String(length=71), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=71), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = 'idempotency-record/v1'",
            name="idempotency_schema_version",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["app.experiments.id"],
            name="fk_idempotency_records_experiment_id_experiments",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id", "job_id"],
            ["app.jobs.experiment_id", "app.jobs.id"],
            name="fk_idempotency_records_experiment_job",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("idempotency_key", name="pk_idempotency_records"),
        sa.UniqueConstraint("job_id", name="uq_idempotency_records_job_id"),
        schema=APP_SCHEMA,
    )
    op.create_table(
        "events",
        sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("previous_status", sa.String(length=32), nullable=True),
        sa.Column("current_status", sa.String(length=32), nullable=False),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint("schema_version = 'job-event/v1'", name="event_schema_version"),
        sa.CheckConstraint(
            "current_status IN ('queued','validating','waiting_approval','running','succeeded',"
            "'failed','cancelled','timed_out')",
            name="event_current_status",
        ),
        sa.CheckConstraint(
            "previous_status IS NULL OR previous_status IN ('queued','validating','waiting_approval',"
            "'running','succeeded','failed','cancelled','timed_out')",
            name="event_previous_status",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["app.experiments.id"],
            name="fk_events_experiment_id_experiments",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id", "job_id"],
            ["app.jobs.experiment_id", "app.jobs.id"],
            name="fk_events_experiment_job",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("sequence", name="pk_events"),
        sa.UniqueConstraint("event_id", name="uq_events_event_id"),
        schema=APP_SCHEMA,
    )
    op.create_index(
        "ix_events_job_sequence",
        "events",
        ["job_id", "sequence"],
        schema=APP_SCHEMA,
    )
    op.create_index(
        "ix_events_experiment_sequence",
        "events",
        ["experiment_id", "sequence"],
        schema=APP_SCHEMA,
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.String(length=64), nullable=True),
        sa.Column("job_id", sa.String(length=64), nullable=True),
        sa.Column("actor", sa.String(length=256), nullable=False),
        sa.Column("action", sa.String(length=256), nullable=False),
        sa.Column("resource_type", sa.String(length=256), nullable=False),
        sa.Column("resource_id", sa.String(length=256), nullable=False),
        sa.Column("request_id", sa.String(length=256), nullable=False),
        sa.Column("decision_id", sa.String(length=256), nullable=True),
        sa.Column("before_artifact_id", sa.String(length=64), nullable=True),
        sa.Column("after_artifact_id", sa.String(length=64), nullable=True),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("schema_version = 'audit-event/v1'", name="audit_schema_version"),
        sa.CheckConstraint(
            "result IN ('succeeded','failed','denied')",
            name="audit_result",
        ),
        sa.CheckConstraint(
            "experiment_id IS NOT NULL OR (job_id IS NULL AND before_artifact_id IS NULL "
            "AND after_artifact_id IS NULL)",
            name="audit_experiment_binding",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["app.experiments.id"],
            name="fk_audit_events_experiment_id_experiments",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id", "job_id"],
            ["app.jobs.experiment_id", "app.jobs.id"],
            name="fk_audit_events_experiment_job",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id", "before_artifact_id"],
            ["app.artifacts.experiment_id", "app.artifacts.id"],
            name="fk_audit_events_experiment_before_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id", "after_artifact_id"],
            ["app.artifacts.experiment_id", "app.artifacts.id"],
            name="fk_audit_events_experiment_after_artifact",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
        schema=APP_SCHEMA,
    )
    op.create_index(
        "ix_audit_events_experiment_occurred",
        "audit_events",
        ["experiment_id", "occurred_at"],
        schema=APP_SCHEMA,
    )
    op.create_index(
        "ix_audit_events_request",
        "audit_events",
        ["request_id"],
        schema=APP_SCHEMA,
    )
    op.execute(
        """
        CREATE FUNCTION app.reject_append_only_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER audit_events_append_only BEFORE UPDATE OR DELETE ON app.audit_events "
        "FOR EACH ROW EXECUTE FUNCTION app.reject_append_only_mutation()"
    )
    op.execute(
        "CREATE TRIGGER events_append_only BEFORE UPDATE OR DELETE ON app.events "
        "FOR EACH ROW EXECUTE FUNCTION app.reject_append_only_mutation()"
    )
    op.execute(
        "CREATE TRIGGER idempotency_records_append_only BEFORE UPDATE OR DELETE "
        "ON app.idempotency_records FOR EACH ROW "
        "EXECUTE FUNCTION app.reject_append_only_mutation()"
    )
    op.execute(
        "CREATE TRIGGER audit_events_append_only_truncate BEFORE TRUNCATE ON app.audit_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION app.reject_append_only_mutation()"
    )
    op.execute(
        "CREATE TRIGGER events_append_only_truncate BEFORE TRUNCATE ON app.events "
        "FOR EACH STATEMENT EXECUTE FUNCTION app.reject_append_only_mutation()"
    )
    op.execute(
        "CREATE TRIGGER idempotency_records_append_only_truncate BEFORE TRUNCATE "
        "ON app.idempotency_records FOR EACH STATEMENT "
        "EXECUTE FUNCTION app.reject_append_only_mutation()"
    )
    op.execute(
        """
        CREATE FUNCTION app.reject_plan_material_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.id IS DISTINCT FROM OLD.id
             OR NEW.experiment_id IS DISTINCT FROM OLD.experiment_id
             OR NEW.kind IS DISTINCT FROM OLD.kind
             OR NEW.schema_version IS DISTINCT FROM OLD.schema_version
             OR NEW.body_json IS DISTINCT FROM OLD.body_json
             OR NEW.plan_hash IS DISTINCT FROM OLD.plan_hash
             OR NEW.risk_level IS DISTINCT FROM OLD.risk_level
             OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'approved Plan material is immutable';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER plans_immutable_material BEFORE UPDATE ON app.plans "
        "FOR EACH ROW EXECUTE FUNCTION app.reject_plan_material_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS plans_immutable_material ON app.plans")
    op.execute("DROP FUNCTION IF EXISTS app.reject_plan_material_mutation()")
    op.execute(
        "DROP TRIGGER IF EXISTS idempotency_records_append_only_truncate "
        "ON app.idempotency_records"
    )
    op.execute("DROP TRIGGER IF EXISTS events_append_only_truncate ON app.events")
    op.execute(
        "DROP TRIGGER IF EXISTS audit_events_append_only_truncate ON app.audit_events"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS idempotency_records_append_only ON app.idempotency_records"
    )
    op.execute("DROP TRIGGER IF EXISTS events_append_only ON app.events")
    op.execute("DROP TRIGGER IF EXISTS audit_events_append_only ON app.audit_events")
    op.execute("DROP FUNCTION IF EXISTS app.reject_append_only_mutation()")
    op.drop_table("audit_events", schema=APP_SCHEMA)
    op.drop_table("events", schema=APP_SCHEMA)
    op.drop_table("idempotency_records", schema=APP_SCHEMA)
    op.drop_table("jobs", schema=APP_SCHEMA)
    op.drop_table("artifacts", schema=APP_SCHEMA)
    op.drop_table("plans", schema=APP_SCHEMA)
    op.drop_table("experiments", schema=APP_SCHEMA)
    op.execute(sa.schema.DropSchema(APP_SCHEMA))
