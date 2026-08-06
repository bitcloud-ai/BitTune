"""Persist M7 Optimization Trial ledger and provision the Optuna schema.

Revision ID: 20260806_0003
Revises: 20260806_0002
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260806_0003"
down_revision: str | None = "20260806_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_SCHEMA = "app"


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS optuna")
    op.create_table(
        "optimization_trials",
        sa.Column("trial_id", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("trial_schema_version", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("plan_hash", sa.String(length=71), nullable=False),
        sa.Column("study_id", sa.String(length=64), nullable=False),
        sa.Column("trial_number", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.String(length=64), nullable=False),
        sa.Column("benchmark_run_id", sa.String(length=64), nullable=False),
        sa.Column(
            "parameters_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "constraints_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "objective_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "provenance_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "evidence_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "error_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "reservation_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("checkpoint_stage", sa.String(length=32), nullable=True),
        sa.Column("provider_resource_id", sa.String(length=256), nullable=True),
        sa.Column(
            "evidence_run_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint(
            "schema_version = 'optimization-trial-entry/v1'",
            name="optimization_trial_schema_version",
        ),
        sa.CheckConstraint(
            "trial_schema_version = 'optimization-trial/v1'",
            name="optimization_trial_record_schema_version",
        ),
        sa.CheckConstraint(
            "status IN ('suggested','rejected_static','deployment_failed','benchmark_failed',"
            "'oom','constraint_failed','completed','cancelled')",
            name="optimization_trial_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(parameters_json) = 'object' "
            "AND jsonb_typeof(constraints_json) = 'array' "
            "AND jsonb_typeof(evidence_json) = 'array' "
            "AND jsonb_typeof(reservation_json) = 'object'",
            name="optimization_trial_json_shapes",
        ),
        sa.CheckConstraint(
            "((checkpoint_stage IS NULL AND provider_resource_id IS NULL) OR "
            "(status = 'suggested' AND checkpoint_stage IN ('deployment','benchmark') "
            "AND provider_resource_id IS NOT NULL))",
            name="optimization_trial_checkpoint",
        ),
        sa.CheckConstraint(
            "((status = 'suggested' AND evidence_run_json IS NULL AND ended_at IS NULL) OR "
            "(status <> 'suggested' AND evidence_run_json IS NOT NULL AND ended_at IS NOT NULL))",
            name="optimization_trial_terminal_evidence",
        ),
        sa.CheckConstraint(
            "((status IN ('completed','constraint_failed') AND objective_json IS NOT NULL "
            "AND provenance_json IS NOT NULL AND jsonb_array_length(constraints_json) > 0) OR "
            "(status NOT IN ('completed','constraint_failed') AND objective_json IS NULL "
            "AND provenance_json IS NULL AND jsonb_array_length(constraints_json) = 0))",
            name="optimization_trial_measured_data",
        ),
        sa.CheckConstraint(
            "((status IN ('rejected_static','deployment_failed','benchmark_failed','oom') "
            "AND error_json IS NOT NULL) OR "
            "(status NOT IN ('rejected_static','deployment_failed','benchmark_failed','oom') "
            "AND error_json IS NULL))",
            name="optimization_trial_error",
        ),
        sa.CheckConstraint(
            "created_at <= updated_at AND (ended_at IS NULL OR updated_at <= ended_at)",
            name="optimization_trial_chronology",
        ),
        sa.CheckConstraint("version >= 1", name="optimization_trial_positive_version"),
        sa.ForeignKeyConstraint(
            ["experiment_id", "plan_id", "plan_hash"],
            ["app.plans.experiment_id", "app.plans.id", "app.plans.plan_hash"],
            name="fk_optimization_trials_plan_material",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("trial_id", name="pk_optimization_trials"),
        sa.UniqueConstraint(
            "experiment_id",
            "study_id",
            "trial_number",
            name="uq_optimization_trials_study_number",
        ),
        sa.UniqueConstraint(
            "experiment_id",
            "trial_id",
            name="uq_optimization_trials_experiment_id",
        ),
        schema=APP_SCHEMA,
    )
    op.create_index(
        "ix_optimization_trials_study_status",
        "optimization_trials",
        ["experiment_id", "study_id", "status", "trial_number"],
        schema=APP_SCHEMA,
    )
    op.execute(
        """
        CREATE FUNCTION app.enforce_optimization_trial_lifecycle()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'Optimization Trial records cannot be deleted';
          END IF;
          IF OLD.status <> 'suggested' THEN
            RAISE EXCEPTION 'terminal Optimization Trial records are immutable';
          END IF;
          IF NEW.trial_id IS DISTINCT FROM OLD.trial_id
             OR NEW.schema_version IS DISTINCT FROM OLD.schema_version
             OR NEW.trial_schema_version IS DISTINCT FROM OLD.trial_schema_version
             OR NEW.experiment_id IS DISTINCT FROM OLD.experiment_id
             OR NEW.plan_id IS DISTINCT FROM OLD.plan_id
             OR NEW.plan_hash IS DISTINCT FROM OLD.plan_hash
             OR NEW.study_id IS DISTINCT FROM OLD.study_id
             OR NEW.trial_number IS DISTINCT FROM OLD.trial_number
             OR NEW.candidate_id IS DISTINCT FROM OLD.candidate_id
             OR NEW.benchmark_run_id IS DISTINCT FROM OLD.benchmark_run_id
             OR NEW.parameters_json IS DISTINCT FROM OLD.parameters_json
             OR NEW.reservation_json IS DISTINCT FROM OLD.reservation_json
             OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'Optimization Trial binding is immutable';
          END IF;
          IF NEW.version <> OLD.version + 1 THEN
            RAISE EXCEPTION 'Optimization Trial version must increase by one';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER optimization_trials_lifecycle BEFORE UPDATE OR DELETE "
        "ON app.optimization_trials FOR EACH ROW "
        "EXECUTE FUNCTION app.enforce_optimization_trial_lifecycle()"
    )
    op.execute(
        "CREATE TRIGGER optimization_trials_no_truncate BEFORE TRUNCATE "
        "ON app.optimization_trials FOR EACH STATEMENT "
        "EXECUTE FUNCTION app.reject_append_only_mutation()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS optimization_trials_no_truncate ON app.optimization_trials"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS optimization_trials_lifecycle ON app.optimization_trials"
    )
    op.execute("DROP FUNCTION IF EXISTS app.enforce_optimization_trial_lifecycle()")
    op.drop_table("optimization_trials", schema=APP_SCHEMA)
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_tables WHERE schemaname = 'optuna'
          ) THEN
            DROP SCHEMA IF EXISTS optuna;
          END IF;
        END;
        $$
        """
    )
