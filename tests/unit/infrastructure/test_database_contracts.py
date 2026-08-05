from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from autopilot.infrastructure.database.base import Base
from autopilot.infrastructure.database.models import (
    ArtifactRow,
    AuditEventRow,
    EventRow,
    IdempotencyRow,
    JobRow,
    PlanRow,
)
from autopilot.infrastructure.database.repositories import claimable_job_statement
from autopilot.infrastructure.database.session import create_postgres_engine


def test_metadata_contains_required_m3_tables_without_artifact_blob() -> None:
    assert set(Base.metadata.tables) == {
        "app.experiments",
        "app.plans",
        "app.jobs",
        "app.artifacts",
        "app.idempotency_records",
        "app.events",
        "app.audit_events",
    }
    assert "content" not in ArtifactRow.__table__.columns
    assert "storage_path" in ArtifactRow.__table__.columns
    assert "schema_version" in ArtifactRow.__table__.columns


def test_job_ddl_uses_postgresql_jsonb_timezone_and_database_constraints() -> None:
    ddl = str(CreateTable(JobRow.__table__).compile(dialect=postgresql.dialect()))

    assert "JSONB" in ddl
    assert "TIMESTAMP WITH TIME ZONE" in ddl
    assert "ck_jobs_job_lease_consistency" in ddl
    assert "ck_jobs_job_lease_by_status" in ddl
    assert "ck_jobs_job_terminal_data" in ddl
    assert "fk_jobs_experiment_plan" in ddl
    assert "fk_jobs_experiment_result_artifact" in ddl


def test_persisted_m3_contracts_store_schema_versions_and_composite_ownership() -> None:
    for row_type in (PlanRow, ArtifactRow, JobRow, IdempotencyRow, EventRow, AuditEventRow):
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
