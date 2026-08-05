"""Persist M4 approval, Tool Set, and Job authorization evidence.

Revision ID: 20260806_0002
Revises: 20260805_0001
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260806_0002"
down_revision: str | None = "20260805_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_SCHEMA = "app"


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_plans_experiment_id_plan_hash",
        "plans",
        ["experiment_id", "id", "plan_hash"],
        schema=APP_SCHEMA,
    )
    op.create_unique_constraint(
        "uq_idempotency_authorization_material",
        "idempotency_records",
        ["idempotency_key", "experiment_id", "job_id", "request_hash", "action"],
        schema=APP_SCHEMA,
    )
    op.create_table(
        "approvals",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("plan_hash", sa.String(length=71), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("risk_level", sa.String(length=2), nullable=False),
        sa.Column("requester_kind", sa.String(length=16), nullable=False),
        sa.Column("requester_id", sa.String(length=64), nullable=False),
        sa.Column("requester_role", sa.String(length=16), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("decided_by_kind", sa.String(length=16), nullable=True),
        sa.Column("decided_by_id", sa.String(length=64), nullable=True),
        sa.Column("decided_by_role", sa.String(length=16), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("comment", sa.String(length=4096), nullable=True),
        sa.CheckConstraint(
            "schema_version = 'approval/v2'",
            name="approval_schema_version",
        ),
        sa.CheckConstraint("risk_level = 'L2'", name="approval_l2_only"),
        sa.CheckConstraint(
            "requester_kind = 'human' AND requester_role IN ('operator','admin')",
            name="approval_human_requester",
        ),
        sa.CheckConstraint(
            "decision IN ('pending','approved','rejected','expired')",
            name="approval_decision",
        ),
        sa.CheckConstraint("requested_at < expires_at", name="approval_expiry"),
        sa.CheckConstraint(
            "((decision IN ('pending','expired') AND decided_by_kind IS NULL "
            "AND decided_by_id IS NULL AND decided_by_role IS NULL AND decided_at IS NULL) OR "
            "(decision IN ('approved','rejected') AND decided_by_kind = 'human' "
            "AND decided_by_id IS NOT NULL AND decided_by_role = 'admin' "
            "AND decided_at IS NOT NULL AND requested_at <= decided_at "
            "AND decided_at < expires_at))",
            name="approval_decision_metadata",
        ),
        sa.CheckConstraint(
            "decided_by_id IS NULL OR requester_id <> decided_by_id",
            name="approval_no_self_decision",
        ),
        sa.CheckConstraint(
            "comment IS NULL OR length(btrim(comment)) BETWEEN 1 AND 4096",
            name="approval_comment",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id", "plan_id", "plan_hash"],
            ["app.plans.experiment_id", "app.plans.id", "app.plans.plan_hash"],
            name="fk_approvals_plan_material",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_approvals"),
        sa.UniqueConstraint(
            "experiment_id",
            "plan_id",
            "plan_hash",
            "action",
            name="uq_approvals_plan_hash_action",
        ),
        sa.UniqueConstraint("experiment_id", "id", name="uq_approvals_experiment_id"),
        schema=APP_SCHEMA,
    )
    op.create_index(
        "ix_approvals_plan_action",
        "approvals",
        ["experiment_id", "plan_id", "action"],
        schema=APP_SCHEMA,
    )
    op.create_table(
        "toolset_snapshots",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("tool_set_version", sa.String(length=71), nullable=False),
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("subject_kind", sa.String(length=16), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("subject_role", sa.String(length=16), nullable=True),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column(
            "hardware_capabilities_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "enabled_providers_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "enabled_feature_flags_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "tools_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "policy_decision_ids_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = 'tool-set-snapshot/v1'",
            name="toolset_snapshot_schema_version",
        ),
        sa.CheckConstraint(
            "((subject_kind = 'human' AND subject_role IN ('viewer','operator','admin')) OR "
            "(subject_kind = 'service' AND subject_role IS NULL))",
            name="toolset_snapshot_subject",
        ),
        sa.CheckConstraint(
            "phase IN ('requirements','environment','planning','approval','deployment',"
            "'benchmark','optimization','verification','report','completed','failed','cancelled')",
            name="toolset_snapshot_phase",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(hardware_capabilities_json) = 'array' "
            "AND jsonb_typeof(enabled_providers_json) = 'array' "
            "AND jsonb_typeof(enabled_feature_flags_json) = 'array' "
            "AND jsonb_typeof(tools_json) = 'array' "
            "AND jsonb_typeof(policy_decision_ids_json) = 'array'",
            name="toolset_snapshot_json_arrays",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["app.experiments.id"],
            name="fk_toolset_snapshots_experiment_id_experiments",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_toolset_snapshots"),
        sa.UniqueConstraint(
            "experiment_id",
            "id",
            name="uq_toolset_snapshots_experiment_id",
        ),
        schema=APP_SCHEMA,
    )
    op.create_index(
        "ix_toolset_snapshots_experiment_created",
        "toolset_snapshots",
        ["experiment_id", "created_at"],
        schema=APP_SCHEMA,
    )
    op.create_table(
        "job_authorizations",
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("subject_kind", sa.String(length=16), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("subject_role", sa.String(length=16), nullable=True),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("risk_level", sa.String(length=2), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("plan_hash", sa.String(length=71), nullable=False),
        sa.Column("approval_id", sa.String(length=64), nullable=True),
        sa.Column("tool_schema_version", sa.String(length=64), nullable=False),
        sa.Column("tool_set_id", sa.String(length=64), nullable=False),
        sa.Column("tool_set_version", sa.String(length=71), nullable=False),
        sa.Column("policy_decision_id", sa.String(length=256), nullable=False),
        sa.Column("request_hash", sa.String(length=71), nullable=False),
        sa.Column("idempotency_key", sa.String(length=71), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = 'job-authorization/v1'",
            name="job_authorization_schema_version",
        ),
        sa.CheckConstraint(
            "risk_level IN ('L0','L1','L2')",
            name="job_authorization_risk_level",
        ),
        sa.CheckConstraint(
            "((risk_level = 'L2' AND approval_id IS NOT NULL) OR "
            "(risk_level IN ('L0','L1') AND approval_id IS NULL))",
            name="job_authorization_approval",
        ),
        sa.CheckConstraint(
            "((subject_kind = 'human' AND subject_role IN ('viewer','operator','admin')) OR "
            "(subject_kind = 'service' AND subject_role IS NULL))",
            name="job_authorization_subject",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id", "job_id"],
            ["app.jobs.experiment_id", "app.jobs.id"],
            name="fk_job_authorizations_job",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id", "plan_id", "plan_hash"],
            ["app.plans.experiment_id", "app.plans.id", "app.plans.plan_hash"],
            name="fk_job_authorizations_plan_material",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id", "approval_id"],
            ["app.approvals.experiment_id", "app.approvals.id"],
            name="fk_job_authorizations_approval",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id", "tool_set_id"],
            ["app.toolset_snapshots.experiment_id", "app.toolset_snapshots.id"],
            name="fk_job_authorizations_toolset",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["idempotency_key", "experiment_id", "job_id", "request_hash", "action"],
            [
                "app.idempotency_records.idempotency_key",
                "app.idempotency_records.experiment_id",
                "app.idempotency_records.job_id",
                "app.idempotency_records.request_hash",
                "app.idempotency_records.action",
            ],
            name="fk_job_authorizations_idempotency_material",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("job_id", name="pk_job_authorizations"),
        sa.UniqueConstraint(
            "experiment_id",
            "job_id",
            name="uq_job_authorizations_experiment_id",
        ),
        schema=APP_SCHEMA,
    )
    op.create_index(
        "ix_job_authorizations_plan_action",
        "job_authorizations",
        ["experiment_id", "plan_id", "action"],
        schema=APP_SCHEMA,
    )
    op.execute(
        """
        CREATE FUNCTION app.enforce_approval_lifecycle()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'Approval records cannot be deleted';
          END IF;
          IF OLD.decision <> 'pending' THEN
            RAISE EXCEPTION 'terminal Approval records are immutable';
          END IF;
          IF NEW.id IS DISTINCT FROM OLD.id
             OR NEW.schema_version IS DISTINCT FROM OLD.schema_version
             OR NEW.experiment_id IS DISTINCT FROM OLD.experiment_id
             OR NEW.plan_id IS DISTINCT FROM OLD.plan_id
             OR NEW.plan_hash IS DISTINCT FROM OLD.plan_hash
             OR NEW.action IS DISTINCT FROM OLD.action
             OR NEW.risk_level IS DISTINCT FROM OLD.risk_level
             OR NEW.requester_kind IS DISTINCT FROM OLD.requester_kind
             OR NEW.requester_id IS DISTINCT FROM OLD.requester_id
             OR NEW.requester_role IS DISTINCT FROM OLD.requester_role
             OR NEW.requested_at IS DISTINCT FROM OLD.requested_at
             OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
            RAISE EXCEPTION 'Approval binding is immutable';
          END IF;
          IF NEW.decision = 'pending' THEN
            RAISE EXCEPTION 'Approval updates must make one terminal transition';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER approvals_lifecycle BEFORE UPDATE OR DELETE ON app.approvals "
        "FOR EACH ROW EXECUTE FUNCTION app.enforce_approval_lifecycle()"
    )
    op.execute(
        "CREATE TRIGGER approvals_no_truncate BEFORE TRUNCATE ON app.approvals "
        "FOR EACH STATEMENT EXECUTE FUNCTION app.reject_append_only_mutation()"
    )
    op.execute(
        "CREATE TRIGGER toolset_snapshots_append_only BEFORE UPDATE OR DELETE "
        "ON app.toolset_snapshots FOR EACH ROW "
        "EXECUTE FUNCTION app.reject_append_only_mutation()"
    )
    op.execute(
        "CREATE TRIGGER toolset_snapshots_append_only_truncate BEFORE TRUNCATE "
        "ON app.toolset_snapshots FOR EACH STATEMENT "
        "EXECUTE FUNCTION app.reject_append_only_mutation()"
    )
    op.execute(
        "CREATE TRIGGER job_authorizations_append_only BEFORE UPDATE OR DELETE "
        "ON app.job_authorizations FOR EACH ROW "
        "EXECUTE FUNCTION app.reject_append_only_mutation()"
    )
    op.execute(
        "CREATE TRIGGER job_authorizations_append_only_truncate BEFORE TRUNCATE "
        "ON app.job_authorizations FOR EACH STATEMENT "
        "EXECUTE FUNCTION app.reject_append_only_mutation()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS job_authorizations_append_only_truncate "
        "ON app.job_authorizations"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS job_authorizations_append_only ON app.job_authorizations"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS toolset_snapshots_append_only_truncate "
        "ON app.toolset_snapshots"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS toolset_snapshots_append_only ON app.toolset_snapshots"
    )
    op.execute("DROP TRIGGER IF EXISTS approvals_no_truncate ON app.approvals")
    op.execute("DROP TRIGGER IF EXISTS approvals_lifecycle ON app.approvals")
    op.execute("DROP FUNCTION IF EXISTS app.enforce_approval_lifecycle()")
    op.drop_table("job_authorizations", schema=APP_SCHEMA)
    op.drop_table("toolset_snapshots", schema=APP_SCHEMA)
    op.drop_table("approvals", schema=APP_SCHEMA)
    op.drop_constraint(
        "uq_idempotency_authorization_material",
        "idempotency_records",
        schema=APP_SCHEMA,
        type_="unique",
    )
    op.drop_constraint(
        "uq_plans_experiment_id_plan_hash",
        "plans",
        schema=APP_SCHEMA,
        type_="unique",
    )
