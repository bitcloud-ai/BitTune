"""Persist M8 Graph State and deployment API projections.

Revision ID: 20260806_0004
Revises: 20260806_0003
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260806_0004"
down_revision: str | None = "20260806_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_SCHEMA = "app"


def upgrade() -> None:
    op.add_column(
        "experiments",
        sa.Column(
            "graph_state_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        schema=APP_SCHEMA,
    )
    op.create_table(
        "deployments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_id", sa.String(length=64), nullable=False),
        sa.Column("container_id", sa.String(length=256), nullable=True),
        sa.Column("endpoint", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("parameter_hash", sa.String(length=71), nullable=False),
        sa.Column("image_digest", sa.String(length=256), nullable=False),
        sa.Column("model_revision", sa.String(length=64), nullable=False),
        sa.Column("gpu_id", sa.Integer(), nullable=False),
        sa.Column("logs_artifact_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("gpu_id = 0", name="deployment_single_gpu_zero"),
        sa.CheckConstraint(
            "schema_version = 'deployment-record/v1'",
            name="deployment_schema_version",
        ),
        sa.CheckConstraint(
            "status IN ('starting','healthy','stopping','stopped','failed')",
            name="deployment_status",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["app.experiments.id"],
            name="fk_deployments_experiment_id_experiments",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deployments"),
        sa.UniqueConstraint("experiment_id", "id", name="uq_deployments_experiment_id"),
        schema=APP_SCHEMA,
    )
    op.create_index(
        "ix_deployments_experiment_updated",
        "deployments",
        ["experiment_id", "updated_at"],
        schema=APP_SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_deployments_experiment_updated",
        table_name="deployments",
        schema=APP_SCHEMA,
    )
    op.drop_table("deployments", schema=APP_SCHEMA)
    op.drop_column("experiments", "graph_state_json", schema=APP_SCHEMA)
