from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from autopilot.infrastructure.database.base import Base
from autopilot.infrastructure.database.models import (
    ApprovalRow,
    ArtifactRow,
    AuditEventRow,
    DeploymentRow,
    EventRow,
    IdempotencyRow,
    JobAuthorizationRow,
    JobRow,
    OptimizationTrialRow,
    PlanRow,
    ToolSetSnapshotRow,
)
from autopilot.infrastructure.database.repositories import claimable_job_statement
from autopilot.infrastructure.database.session import create_postgres_engine


def test_metadata_contains_required_tables_without_artifact_blob() -> None:
    assert set(Base.metadata.tables) == {
        "app.experiments",
        "app.plans",
        "app.approvals",
        "app.toolset_snapshots",
        "app.jobs",
        "app.artifacts",
        "app.idempotency_records",
        "app.job_authorizations",
        "app.events",
        "app.audit_events",
        "app.optimization_trials",
        "app.deployments",
    }
    assert "content" not in ArtifactRow.__table__.columns
    assert "storage_path" in ArtifactRow.__table__.columns
    assert "schema_version" in ArtifactRow.__table__.columns
    assert "schema_version" in DeploymentRow.__table__.columns


def test_job_ddl_uses_postgresql_jsonb_timezone_and_database_constraints() -> None:
    ddl = str(CreateTable(JobRow.__table__).compile(dialect=postgresql.dialect()))

    assert "JSONB" in ddl
    assert "TIMESTAMP WITH TIME ZONE" in ddl
    assert "ck_jobs_job_lease_consistency" in ddl
    assert "ck_jobs_job_lease_by_status" in ddl
    assert "ck_jobs_job_terminal_data" in ddl
    assert "fk_jobs_experiment_plan" in ddl
    assert "fk_jobs_experiment_result_artifact" in ddl


def test_approval_ddl_enforces_l2_binding_lifecycle_and_actor_separation() -> None:
    ddl = str(CreateTable(ApprovalRow.__table__).compile(dialect=postgresql.dialect()))

    assert "TIMESTAMP WITH TIME ZONE" in ddl
    assert "ck_approvals_approval_schema_version" in ddl
    assert "ck_approvals_approval_l2_only" in ddl
    assert "ck_approvals_approval_human_requester" in ddl
    assert "ck_approvals_approval_decision_metadata" in ddl
    assert "ck_approvals_approval_no_self_decision" in ddl
    assert "fk_approvals_plan_material" in ddl
    assert "uq_approvals_plan_hash_action" in ddl


def test_gateway_evidence_ddl_enforces_composite_authorization_bindings() -> None:
    toolset_ddl = str(
        CreateTable(ToolSetSnapshotRow.__table__).compile(dialect=postgresql.dialect())
    )
    authorization_ddl = str(
        CreateTable(JobAuthorizationRow.__table__).compile(dialect=postgresql.dialect())
    )

    assert "JSONB" in toolset_ddl
    assert "ck_toolset_snapshots_toolset_snapshot_subject" in toolset_ddl
    assert "fk_job_authorizations_plan_material" in authorization_ddl
    assert "fk_job_authorizations_approval" in authorization_ddl
    assert "fk_job_authorizations_toolset" in authorization_ddl
    assert "fk_job_authorizations_idempotency_material" in authorization_ddl
    assert "DEFERRABLE INITIALLY DEFERRED" in authorization_ddl


def test_optimization_trial_ddl_enforces_restart_and_terminal_evidence_contract() -> None:
    ddl = str(CreateTable(OptimizationTrialRow.__table__).compile(dialect=postgresql.dialect()))

    assert "JSONB" in ddl
    assert "ck_optimization_trials_optimization_trial_checkpoint" in ddl
    assert "ck_optimization_trials_optimization_trial_terminal_evidence" in ddl
    assert "ck_optimization_trials_optimization_trial_measured_data" in ddl
    assert "fk_optimization_trials_plan_material" in ddl


def test_persisted_m3_contracts_store_schema_versions_and_composite_ownership() -> None:
    for row_type in (
        PlanRow,
        ArtifactRow,
        ToolSetSnapshotRow,
        JobRow,
        IdempotencyRow,
        JobAuthorizationRow,
        EventRow,
        AuditEventRow,
        OptimizationTrialRow,
    ):
        assert "schema_version" in row_type.__table__.columns

    job_foreign_keys = {constraint.name for constraint in JobRow.__table__.foreign_key_constraints}
    assert "fk_jobs_experiment_plan" in job_foreign_keys
    assert "fk_jobs_experiment_result_artifact" in job_foreign_keys
    assert "lease_generation" in JobRow.__table__.columns


def test_claim_query_uses_skip_locked_and_expired_lease_filter() -> None:
    statement = claimable_job_statement()
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "clock_timestamp()" in sql
    assert "lease_expires_at" in sql
    assert "waiting_approval" in sql
    assert "ORDER BY app.jobs.submitted_at, app.jobs.id" in sql


def test_idempotency_job_foreign_key_is_deferred() -> None:
    job_key = next(
        key for key in IdempotencyRow.__table__.foreign_keys if key.parent.name == "job_id"
    )

    assert job_key.deferrable is True
    assert job_key.initially == "DEFERRED"


def test_engine_factory_rejects_non_postgresql_database() -> None:
    with pytest.raises(ValueError, match=r"postgresql\+psycopg"):
        create_postgres_engine("sqlite+pysqlite:///:memory:")


def test_initial_migration_enforces_immutable_ledger_tables_and_plans() -> None:
    migration = (
        Path(__file__).parents[3] / "alembic" / "versions" / "20260805_0001_initial_app_schema.py"
    ).read_text(encoding="utf-8")

    assert "audit_events_append_only" in migration
    assert "events_append_only" in migration
    assert "idempotency_records_append_only" in migration
    assert "append_only_truncate" in migration
    assert "plans_immutable_material" in migration
    assert "artifact_schema_version" in migration
    assert 'name="ck_jobs_' not in migration


def test_approval_migration_enforces_single_terminal_transition_and_no_deletion() -> None:
    migration = (
        Path(__file__).parents[3] / "alembic" / "versions" / "20260806_0002_approval_persistence.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision: str | None = "20260805_0001"' in migration
    assert "enforce_approval_lifecycle" in migration
    assert "terminal Approval records are immutable" in migration
    assert "approvals_no_truncate" in migration
    assert "schema_version = 'approval/v2'" in migration
    assert "requester_kind = 'human'" in migration
    assert "decided_by_role = 'admin'" in migration
    assert "uq_approvals_plan_hash_action" in migration
    assert "fk_approvals_plan_material" in migration
    assert "toolset_snapshots_append_only" in migration
    assert "job_authorizations_append_only" in migration
    assert "fk_job_authorizations_idempotency_material" in migration


def test_optimization_trial_migration_provisions_optuna_schema_and_lifecycle() -> None:
    migration = (
        Path(__file__).parents[3]
        / "alembic"
        / "versions"
        / "20260806_0003_optimization_trial_ledger.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision: str | None = "20260806_0002"' in migration
    assert "CREATE SCHEMA IF NOT EXISTS optuna" in migration
    assert "optimization_trials_lifecycle" in migration
    assert "optimization_trials_no_truncate" in migration
    assert "terminal Optimization Trial records are immutable" in migration
    assert "fk_optimization_trials_plan_material" in migration
