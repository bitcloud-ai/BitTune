from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import func, inspect, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from autopilot.domain.artifacts import ArtifactProducer
from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.enums import ExperimentPhase, JobKind, JobStatus, RiskLevel, UserRole
from autopilot.domain.identifiers import (
    ArtifactId,
    AuditEventId,
    ExperimentId,
    JobId,
    PlanHash,
    PlanId,
    Sha256Digest,
    ToolName,
    UserId,
    WorkerId,
)
from autopilot.domain.identities import HumanSubject
from autopilot.domain.jobs import JobRecord
from autopilot.domain.plans import PlanExecutionRequest
from autopilot.domain.requirements import RequirementSpec
from autopilot.evidence.models import ArtifactMetadata, artifact_storage_path
from autopilot.gateway.models import (
    GatewayEnvironment,
    JobAuthorizationDraft,
    ToolCallRequest,
    ToolSetEntry,
    VisibilityContext,
    create_toolset_snapshot,
)
from autopilot.gateway.mvp_tools import (
    DomainPlanResult,
    JobQuery,
    JobQueryResult,
    JobSubmissionResult,
    provider_statuses,
)
from autopilot.gateway.runtime import build_production_gateway
from autopilot.infrastructure.database.base import APP_SCHEMA, Base
from autopilot.infrastructure.database.errors import (
    ArtifactBindingError,
    LeaseConflictError,
    PlanBindingError,
)
from autopilot.infrastructure.database.gateway_repositories import (
    SqlAlchemyToolSetSnapshotRepository,
)
from autopilot.infrastructure.database.models import (
    AuditEventRow,
    EventRow,
    ExperimentRow,
    IdempotencyRow,
    JobAuthorizationRow,
    JobRow,
    PlanRow,
    ToolSetSnapshotRow,
)
from autopilot.infrastructure.database.repositories import (
    SqlAlchemyArtifactRepository,
    SqlAlchemyAuditRepository,
    SqlAlchemyJobRepository,
)
from autopilot.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from autopilot.jobs.models import AuditEvent, AuditResult, JobEventType, JobTransition
from autopilot.policy.models import (
    PolicyDecision,
    PolicyInput,
    PolicyReasonCode,
    PolicyRequirements,
)

_DATABASE_URL = os.environ.get("AUTOPILOT_TEST_POSTGRES_URL")
_PROJECT_ROOT = Path(__file__).parents[2]


class AllowPolicy:
    def evaluate(self, policy_input: PolicyInput) -> PolicyDecision:
        return PolicyDecision(
            allow=True,
            reason_code=PolicyReasonCode.ALLOW,
            requirements=PolicyRequirements(human_approval=False),
            decision_id=f"allow-{policy_input.request_id}",
        )


def _alembic_config(connection: object) -> Config:
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


@pytest.fixture(scope="module")
def postgres_engine() -> Engine:
    if _DATABASE_URL is None:
        pytest.skip("AUTOPILOT_TEST_POSTGRES_URL is not configured")
    engine = create_postgres_engine(_DATABASE_URL)
    version_table_existed = False
    with engine.connect() as connection:
        inspector = inspect(connection)
        if inspector.has_schema(APP_SCHEMA):
            pytest.fail("PostgreSQL integration tests require a database without the app schema")
        version_rows = ()
        version_table_existed = inspector.has_table("alembic_version")
        if version_table_existed:
            version_rows = tuple(
                connection.execute(text("SELECT version_num FROM alembic_version"))
            )
        if version_rows:
            pytest.fail("PostgreSQL integration tests require an unversioned disposable database")
    yield engine
    with engine.begin() as connection:
        if not version_table_existed and inspect(connection).has_table("alembic_version"):
            connection.execute(text("DROP TABLE alembic_version"))
    engine.dispose()


@pytest.fixture(autouse=True)
def migrated_database(postgres_engine: Engine):
    with postgres_engine.connect() as connection:
        command.upgrade(_alembic_config(connection), "head")
    try:
        yield
    finally:
        with postgres_engine.connect() as connection:
            command.downgrade(_alembic_config(connection), "base")


@pytest.fixture
def session_factory(postgres_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(postgres_engine)


def _digest(value: str) -> Sha256Digest:
    return Sha256Digest(root=f"sha256:{hashlib.sha256(value.encode()).hexdigest()}")


def _seed_experiment_and_plan(
    session: Session,
    *,
    kind: JobKind = JobKind.BENCHMARK,
    experiment_id: ExperimentId | None = None,
) -> tuple[ExperimentId, PlanId]:
    now = datetime.now(UTC)
    actual_experiment_id = experiment_id or ExperimentId.new()
    plan_id = PlanId.new()
    if experiment_id is None:
        session.add(
            ExperimentRow(
                id=str(actual_experiment_id),
                status="active",
                phase="benchmark",
                created_by="integration-test",
                requirements_json={},
                created_at=now,
                updated_at=now,
            )
        )
    session.add(
        PlanRow(
            id=str(plan_id),
            experiment_id=str(actual_experiment_id),
            kind=kind.value,
            schema_version=f"{kind.value}-plan/v1",
            body_json={},
            plan_hash=str(_digest(str(plan_id))),
            risk_level="L1",
            status="approved",
            approved_by="integration-test",
            created_at=now,
        )
    )
    session.flush()
    return actual_experiment_id, plan_id


def _queued_job(experiment_id: ExperimentId, plan_id: PlanId) -> JobRecord:
    return JobRecord(
        job_id=JobId.new(),
        experiment_id=experiment_id,
        plan_id=plan_id,
        kind=JobKind.BENCHMARK,
        status=JobStatus.QUEUED,
        submitted_at=datetime.now(UTC),
    )


def _enqueue(
    session: Session,
    job: JobRecord,
    *,
    key: Sha256Digest | None = None,
):
    idempotency_key = key or _digest(str(job.job_id))
    request_hash = _digest(f"request:{job.plan_id}")
    action = ToolName(root="start_benchmark")
    subject = HumanSubject(user_id=UserId.new(), role=UserRole.OPERATOR)
    context = VisibilityContext(
        experiment_id=job.experiment_id,
        subject=subject,
        phase=ExperimentPhase.BENCHMARK,
        enabled_providers=frozenset({"evalscope"}),
    )
    snapshot = create_toolset_snapshot(
        context=context,
        tools=(
            ToolSetEntry(
                name=action,
                schema_version="plan-execution-request/v1",
                risk_level=RiskLevel.L1,
            ),
        ),
        policy_decision_ids=("integration-policy-allow",),
        created_at=job.submitted_at,
    )
    SqlAlchemyToolSetSnapshotRepository(session).add(snapshot)
    plan = session.get(PlanRow, str(job.plan_id))
    assert plan is not None
    return SqlAlchemyJobRepository(session).enqueue(
        job,
        authorization=JobAuthorizationDraft(
            experiment_id=job.experiment_id,
            subject=subject,
            action=action,
            risk_level=RiskLevel.L1,
            plan_id=job.plan_id,
            plan_hash=PlanHash(root=plan.plan_hash),
            tool_schema_version="plan-execution-request/v1",
            tool_set_id=snapshot.tool_set_id,
            tool_set_version=snapshot.tool_set_version,
            policy_decision_id="integration-policy-allow",
            request_hash=request_hash,
            idempotency_key=idempotency_key,
            authorized_at=job.submitted_at,
        ),
    )


def test_alembic_upgrade_matches_sqlalchemy_metadata(postgres_engine: Engine) -> None:
    with postgres_engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"include_schemas": True, "compare_type": True},
        )
        assert compare_metadata(context, Base.metadata) == []

        inspector = inspect(connection)
        assert set(inspector.get_table_names(schema=APP_SCHEMA)) == {
            "experiments",
            "plans",
            "approvals",
            "artifacts",
            "deployments",
            "optimization_trials",
            "toolset_snapshots",
            "jobs",
            "idempotency_records",
            "job_authorizations",
            "events",
            "audit_events",
        }


def test_concurrent_idempotency_creates_one_job_and_event(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory.begin() as session:
        experiment_id, plan_id = _seed_experiment_and_plan(session)
    first_job = _queued_job(experiment_id, plan_id)
    second_job = _queued_job(experiment_id, plan_id)
    key = _digest("shared-idempotency-key")
    barrier = Barrier(2)

    def enqueue_job(job: JobRecord):
        with session_factory.begin() as session:
            barrier.wait()
            return _enqueue(session, job, key=key)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(enqueue_job, (first_job, second_job)))

    assert sorted(result.created for result in results) == [False, True]
    assert len({result.job.job_id for result in results}) == 1
    persisted_job_id = results[0].job.job_id
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(JobRow)) == 1
        assert session.scalar(select(func.count()).select_from(IdempotencyRow)) == 1
        assert session.scalar(select(func.count()).select_from(JobAuthorizationRow)) == 1
        assert session.scalar(select(func.count()).select_from(ToolSetSnapshotRow)) == 2
        assert (
            session.scalar(
                select(func.count())
                .select_from(EventRow)
                .where(EventRow.job_id == str(persisted_job_id))
            )
            == 1
        )


def test_skip_locked_never_claims_the_same_job_twice(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory.begin() as session:
        experiment_id, plan_id = _seed_experiment_and_plan(session)
        job = _queued_job(experiment_id, plan_id)
        _enqueue(session, job)

    first_session = session_factory()
    second_session = session_factory()
    try:
        first_claim = SqlAlchemyJobRepository(first_session).claim_next(
            worker_id=WorkerId.new(),
            lease_duration=timedelta(minutes=1),
        )
        second_claim = SqlAlchemyJobRepository(second_session).claim_next(
            worker_id=WorkerId.new(),
            lease_duration=timedelta(minutes=1),
        )
        assert first_claim is not None
        assert second_claim is None
    finally:
        first_session.rollback()
        second_session.rollback()
        first_session.close()
        second_session.close()


def test_waiting_approval_recovers_with_fencing_and_database_time(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory.begin() as session:
        experiment_id, plan_id = _seed_experiment_and_plan(session)
        job = _queued_job(experiment_id, plan_id)
        _enqueue(session, job)

    worker_id = WorkerId.new()
    with session_factory.begin() as session:
        first_claim = SqlAlchemyJobRepository(session).claim_next(
            worker_id=worker_id,
            lease_duration=timedelta(minutes=5),
        )
        assert first_claim is not None

    stale_event_time = datetime(2000, 1, 1, tzinfo=UTC)
    with session_factory.begin() as session:
        repository = SqlAlchemyJobRepository(session)
        repository.transition(
            job_id=job.job_id,
            transition=JobTransition(
                target=JobStatus.VALIDATING,
                occurred_at=stale_event_time,
            ),
            worker_id=worker_id,
            fencing_token=first_claim.lease.fencing_token,
        )
        repository.transition(
            job_id=job.job_id,
            transition=JobTransition(
                target=JobStatus.WAITING_APPROVAL,
                occurred_at=stale_event_time,
            ),
            worker_id=worker_id,
            fencing_token=first_claim.lease.fencing_token,
        )

    with session_factory.begin() as session:
        session.execute(
            update(JobRow)
            .where(JobRow.id == str(job.job_id))
            .values(
                lease_acquired_at=func.clock_timestamp() - text("interval '10 seconds'"),
                lease_heartbeat_at=func.clock_timestamp() - text("interval '2 seconds'"),
                lease_expires_at=func.clock_timestamp() - text("interval '1 second'"),
            )
        )

    with session_factory.begin() as session:
        recovered = SqlAlchemyJobRepository(session).claim_next(
            worker_id=worker_id,
            lease_duration=timedelta(minutes=5),
        )
        assert recovered is not None
        assert recovered.job.status is JobStatus.WAITING_APPROVAL
        assert recovered.lease.recovered is True
        assert recovered.lease.fencing_token == first_claim.lease.fencing_token + 1

    with (
        session_factory.begin() as session,
        pytest.raises(LeaseConflictError, match="fencing token"),
    ):
        SqlAlchemyJobRepository(session).heartbeat(
            job_id=job.job_id,
            worker_id=worker_id,
            fencing_token=first_claim.lease.fencing_token,
            lease_duration=timedelta(minutes=5),
        )

    with session_factory.begin() as session:
        repository = SqlAlchemyJobRepository(session)
        cancellation = repository.request_cancel(job_id=job.job_id)
        assert cancellation.cancel_requested_at is not None
        running = repository.transition(
            job_id=job.job_id,
            transition=JobTransition(
                target=JobStatus.RUNNING,
                occurred_at=stale_event_time,
                provider_job_id="runner-job-1",
            ),
            worker_id=worker_id,
            fencing_token=recovered.lease.fencing_token,
        )
        assert running.started_at is not None
        assert running.started_at > job.submitted_at

    with session_factory() as session:
        repository = SqlAlchemyJobRepository(session)
        restored = repository.get(job.job_id)
        assert restored is not None
        assert restored.provider_job_id == "runner-job-1"
        assert restored.cancel_requested_at is not None
        assert JobEventType.LEASE_RECOVERED in {
            event.event_type for event in repository.list_events(job.job_id)
        }
        assert JobEventType.CANCEL_REQUESTED in {
            event.event_type for event in repository.list_events(job.job_id)
        }


def test_repository_rejects_cross_experiment_plan_and_artifact(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory.begin() as session:
        first_experiment, first_plan = _seed_experiment_and_plan(session)
        second_experiment, second_plan = _seed_experiment_and_plan(session)
        cross_plan_job = _queued_job(first_experiment, second_plan)
        with pytest.raises(PlanBindingError):
            _enqueue(session, cross_plan_job)

        job = _queued_job(first_experiment, first_plan)
        _enqueue(session, job)

    worker_id = WorkerId.new()
    with session_factory.begin() as session:
        claimed = SqlAlchemyJobRepository(session).claim_next(
            worker_id=worker_id,
            lease_duration=timedelta(minutes=5),
        )
        assert claimed is not None
    with session_factory.begin() as session:
        repository = SqlAlchemyJobRepository(session)
        repository.transition(
            job_id=job.job_id,
            transition=JobTransition(
                target=JobStatus.VALIDATING,
                occurred_at=datetime.now(UTC),
            ),
            worker_id=worker_id,
            fencing_token=claimed.lease.fencing_token,
        )
        repository.transition(
            job_id=job.job_id,
            transition=JobTransition(
                target=JobStatus.RUNNING,
                occurred_at=datetime.now(UTC),
            ),
            worker_id=worker_id,
            fencing_token=claimed.lease.fencing_token,
        )

    artifact_id = ArtifactId.new()
    payload = b"cross-experiment"
    metadata = ArtifactMetadata(
        artifact_id=artifact_id,
        experiment_id=second_experiment,
        category="benchmark",
        content_type="application/json",
        size_bytes=len(payload),
        sha256=_digest(payload.decode()),
        created_at=datetime.now(UTC),
        producer=ArtifactProducer(component="integration-test", version="1.0.0"),
        storage_path=artifact_storage_path(second_experiment, "benchmark", artifact_id),
    )
    with session_factory.begin() as session:
        SqlAlchemyArtifactRepository(session).add(metadata)

    with session_factory.begin() as session, pytest.raises(ArtifactBindingError):
        SqlAlchemyJobRepository(session).transition(
            job_id=job.job_id,
            transition=JobTransition(
                target=JobStatus.SUCCEEDED,
                occurred_at=datetime.now(UTC),
                result_artifact=metadata.to_ref(),
            ),
            worker_id=worker_id,
            fencing_token=claimed.lease.fencing_token,
        )


def test_event_audit_authorization_and_plan_material_are_immutable(
    session_factory: sessionmaker[Session],
    postgres_engine: Engine,
) -> None:
    with session_factory.begin() as session:
        experiment_id, plan_id = _seed_experiment_and_plan(session)
        job = _queued_job(experiment_id, plan_id)
        _enqueue(session, job)
        SqlAlchemyAuditRepository(session).append(
            AuditEvent(
                audit_event_id=AuditEventId.new(),
                experiment_id=experiment_id,
                actor="integration-test",
                action="start_benchmark",
                resource_type="experiment",
                resource_id=str(experiment_id),
                request_id="request-1",
                result=AuditResult.SUCCEEDED,
                occurred_at=datetime.now(UTC),
            )
        )

    statements = (
        "UPDATE app.events SET payload_json = '{}'::jsonb",
        "DELETE FROM app.audit_events",
        "UPDATE app.idempotency_records SET action = 'cancel_benchmark'",
        "TRUNCATE app.events",
        "TRUNCATE app.audit_events",
        "TRUNCATE app.idempotency_records",
        "UPDATE app.toolset_snapshots SET tools_json = '[]'::jsonb",
        "DELETE FROM app.job_authorizations",
        "TRUNCATE app.toolset_snapshots",
        "TRUNCATE app.job_authorizations",
        "UPDATE app.plans SET body_json = '{\"changed\": true}'::jsonb",
    )
    for statement in statements:
        with postgres_engine.connect() as connection:
            transaction = connection.begin()
            with pytest.raises(DBAPIError):
                connection.execute(text(statement))
            transaction.rollback()

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(EventRow)) == 1
        assert session.scalar(select(func.count()).select_from(AuditEventRow)) == 1
        assert session.scalar(select(func.count()).select_from(IdempotencyRow)) == 1
        assert session.scalar(select(func.count()).select_from(ToolSetSnapshotRow)) == 1
        assert session.scalar(select(func.count()).select_from(JobAuthorizationRow)) == 1


def test_production_gateway_persists_environment_plan_and_idempotent_job(
    session_factory: sessionmaker[Session],
) -> None:
    now = datetime(2026, 8, 6, 8, 0, tzinfo=UTC)
    subject = HumanSubject(user_id=UserId.new(), role=UserRole.OPERATOR)
    requirements = RequirementSpec.model_validate(
        {
            "created_by": str(subject.user_id),
            "model_ref": {
                "type": "huggingface",
                "repository_id": "Qwen/Qwen3-8B",
                "revision": "a" * 40,
            },
            "priority": "throughput",
            "workload": {
                "dataset": {"type": "synthetic_fixed", "dataset_id": "medium-v1"},
                "tokenizer": {
                    "repository_id": "Qwen/Qwen3-8B",
                    "revision": "a" * 40,
                },
                "prompt_tokens": 2048,
                "output_tokens": 512,
                "stream": True,
                "ignore_eos": False,
                "sampling": {"temperature": 0, "seed": 7},
            },
            "slo": {
                "constraints": [
                    {
                        "kind": "numeric",
                        "metric": "ttft_p95_ms",
                        "operator": "<=",
                        "value": 2000,
                    }
                ]
            },
            "budget": {
                "max_duration_seconds": 600,
                "max_requests": 100,
                "max_input_tokens": 1_000_000,
                "max_output_tokens": 100_000,
                "max_disk_growth_bytes": 1_000_000_000,
            },
            "allow_model_download": True,
            "allow_container_start": True,
        }
    )
    experiment_id = ExperimentId.new()
    with session_factory.begin() as session:
        session.add(
            ExperimentRow(
                id=str(experiment_id),
                status="active",
                phase="environment",
                created_by=str(subject.user_id),
                requirements_json=requirements.model_dump(mode="json"),
                graph_state_json={"status": "active", "phase": "environment"},
                created_at=now,
                updated_at=now,
            )
        )

    assembly = build_production_gateway(
        sessions=session_factory,
        policy=AllowPolicy(),
        budget_ceiling=ExecutionBudget(
            max_duration_seconds=1800,
            max_requests=10_000,
            max_input_tokens=40_000_000,
            max_output_tokens=10_000_000,
            max_disk_growth_bytes=20_000_000_000,
        ),
        provider_statuses=provider_statuses(verified=("nvml",)),
        clock=lambda: now,
    )
    environment = GatewayEnvironment(
        experiment_id=experiment_id,
        subject=subject,
        enabled_providers=frozenset({"nvml"}),
    )
    snapshot = assembly.gateway.available_tools(
        environment=environment,
        request_id="integration-visible-tools",
    )
    create_plan = next(
        entry for entry in snapshot.tools if str(entry.name) == "create_environment_plan"
    )
    plan_result = assembly.gateway.invoke(
        request=ToolCallRequest(
            request_id="integration-create-plan",
            tool_name=create_plan.name,
            tool_set_id=snapshot.tool_set_id,
            expected_tool_set_version=snapshot.tool_set_version,
            arguments={
                "specification": {
                    "provider_version": "13.610.43",
                    "adapter_version": "environment-adapter-v1",
                    "provider_profile_version": "rtx5090-v1",
                    "scope": "mvp_full",
                    "include_runtime_probe": True,
                }
            },
        ),
        environment=environment,
    )
    assert isinstance(plan_result, DomainPlanResult)
    assert plan_result.status.value == "approved"
    assert plan_result.requires_approval is False

    start_tool = next(
        entry for entry in snapshot.tools if str(entry.name) == "start_environment_inspection"
    )
    start_arguments = PlanExecutionRequest(
        plan_id=plan_result.plan_id,
        expected_plan_hash=plan_result.plan_hash,
    ).model_dump(mode="json")
    first = assembly.gateway.invoke(
        request=ToolCallRequest(
            request_id="integration-start-environment",
            tool_name=start_tool.name,
            tool_set_id=snapshot.tool_set_id,
            expected_tool_set_version=snapshot.tool_set_version,
            arguments=start_arguments,
        ),
        environment=environment,
    )
    replay = assembly.gateway.invoke(
        request=ToolCallRequest(
            request_id="integration-replay-environment",
            tool_name=start_tool.name,
            tool_set_id=snapshot.tool_set_id,
            expected_tool_set_version=snapshot.tool_set_version,
            arguments=start_arguments,
        ),
        environment=environment,
    )

    assert isinstance(first, JobSubmissionResult)
    assert isinstance(replay, JobSubmissionResult)
    assert first.created is True
    assert replay.created is False
    assert replay.job_id == first.job_id
    status_tool = next(
        entry for entry in snapshot.tools if str(entry.name) == "get_environment_status"
    )
    status_result = assembly.gateway.invoke(
        request=ToolCallRequest(
            request_id="integration-status-environment",
            tool_name=status_tool.name,
            tool_set_id=snapshot.tool_set_id,
            expected_tool_set_version=snapshot.tool_set_version,
            arguments=JobQuery(job_id=first.job_id).model_dump(mode="json"),
        ),
        environment=environment,
    )
    assert isinstance(status_result, JobQueryResult)
    assert status_result.job.job_id == first.job_id
    assert status_result.job.experiment_id == experiment_id
    with session_factory() as session:
        plan = session.get(PlanRow, str(plan_result.plan_id))
        assert plan is not None
        assert plan.body_json["execution_specification"]["inspection"]["scope"] == "mvp_full"
        assert session.scalar(select(func.count()).select_from(JobRow)) == 1
        assert session.scalar(select(func.count()).select_from(JobAuthorizationRow)) == 1
