from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.enums import ExperimentPhase, PlanStatus, RiskLevel, UserRole
from autopilot.domain.identifiers import (
    ExperimentId,
    JobId,
    PlanHash,
    PlanId,
    Sha256Digest,
    ToolName,
    UserId,
)
from autopilot.domain.identities import HumanSubject
from autopilot.gateway.errors import IdempotencyAuthorizationError, PlanAuthorizationError
from autopilot.gateway.models import (
    JobAuthorizationDraft,
    ToolSetEntry,
    ToolSetSnapshot,
    VisibilityContext,
    bind_job_authorization,
    create_toolset_snapshot,
)
from autopilot.infrastructure.database.errors import AuthorizationBindingError
from autopilot.infrastructure.database.gateway_repositories import (
    SqlAlchemyJobAuthorizationRepository,
    SqlAlchemyJobIdempotencyGate,
    SqlAlchemyPlanAuthorizationRepository,
    SqlAlchemyToolSetSnapshotRepository,
)
from autopilot.infrastructure.database.models import (
    IdempotencyRow,
    JobAuthorizationRow,
    JobRow,
    PlanRow,
    ToolSetSnapshotRow,
)

NOW = datetime(2026, 8, 6, 9, 0, tzinfo=UTC)
ACTION = ToolName(root="start_benchmark")
PLAN_HASH = PlanHash(root=f"sha256:{'1' * 64}")
REQUEST_HASH = Sha256Digest(root=f"sha256:{'2' * 64}")
IDEMPOTENCY_KEY = Sha256Digest(root=f"sha256:{'3' * 64}")


def _session() -> MagicMock:
    return MagicMock(spec=Session)


def _budget() -> ExecutionBudget:
    return ExecutionBudget(
        max_duration_seconds=60,
        max_requests=100,
        max_input_tokens=1_000,
        max_output_tokens=1_000,
        max_disk_growth_bytes=10_000,
    )


def _snapshot() -> ToolSetSnapshot:
    subject = HumanSubject(user_id=UserId.new(), role=UserRole.OPERATOR)
    context = VisibilityContext(
        experiment_id=ExperimentId.new(),
        subject=subject,
        phase=ExperimentPhase.BENCHMARK,
        hardware_capabilities=frozenset({"single_gpu"}),
        enabled_providers=frozenset({"evalscope"}),
        enabled_feature_flags=frozenset({"benchmark"}),
    )
    return create_toolset_snapshot(
        context=context,
        tools=(
            ToolSetEntry(
                name=ACTION,
                schema_version="plan-execution-request/v1",
                risk_level=RiskLevel.L1,
            ),
        ),
        policy_decision_ids=("opa-visible-1",),
        created_at=NOW,
    )


def _snapshot_row(snapshot: ToolSetSnapshot) -> ToolSetSnapshotRow:
    subject = snapshot.subject
    assert isinstance(subject, HumanSubject)
    return ToolSetSnapshotRow(
        id=str(snapshot.tool_set_id),
        schema_version=snapshot.schema_version,
        tool_set_version=str(snapshot.tool_set_version),
        experiment_id=str(snapshot.experiment_id),
        subject_kind=subject.kind.value,
        subject_id=str(subject.user_id),
        subject_role=subject.role.value,
        phase=snapshot.phase.value,
        hardware_capabilities_json=list(snapshot.hardware_capabilities),
        enabled_providers_json=list(snapshot.enabled_providers),
        enabled_feature_flags_json=list(snapshot.enabled_feature_flags),
        tools_json=[entry.model_dump(mode="json") for entry in snapshot.tools],
        policy_decision_ids_json=list(snapshot.policy_decision_ids),
        created_at=snapshot.created_at,
    )


def _draft(snapshot: ToolSetSnapshot) -> JobAuthorizationDraft:
    return JobAuthorizationDraft(
        experiment_id=snapshot.experiment_id,
        subject=snapshot.subject,
        action=ACTION,
        risk_level=RiskLevel.L1,
        plan_id=PlanId.new(),
        plan_hash=PLAN_HASH,
        tool_schema_version="plan-execution-request/v1",
        tool_set_id=snapshot.tool_set_id,
        tool_set_version=snapshot.tool_set_version,
        policy_decision_id="opa-execution-1",
        request_hash=REQUEST_HASH,
        idempotency_key=IDEMPOTENCY_KEY,
        authorized_at=NOW,
    )


def _authorization_row(job_id: JobId, draft: JobAuthorizationDraft) -> JobAuthorizationRow:
    subject = draft.subject
    assert isinstance(subject, HumanSubject)
    return JobAuthorizationRow(
        job_id=str(job_id),
        schema_version="job-authorization/v1",
        experiment_id=str(draft.experiment_id),
        subject_kind=subject.kind.value,
        subject_id=str(subject.user_id),
        subject_role=subject.role.value,
        action=str(draft.action),
        risk_level=draft.risk_level.value,
        plan_id=str(draft.plan_id),
        plan_hash=str(draft.plan_hash),
        approval_id=None,
        tool_schema_version=draft.tool_schema_version,
        tool_set_id=str(draft.tool_set_id),
        tool_set_version=str(draft.tool_set_version),
        policy_decision_id=draft.policy_decision_id,
        request_hash=str(draft.request_hash),
        idempotency_key=str(draft.idempotency_key),
        authorized_at=draft.authorized_at,
    )


def test_toolset_repository_round_trips_exact_visibility_snapshot() -> None:
    session = _session()
    snapshot = _snapshot()
    session.get.return_value = _snapshot_row(snapshot)

    restored = SqlAlchemyToolSetSnapshotRepository(session).get(snapshot.tool_set_id)

    assert restored == snapshot


def test_toolset_repository_rejects_same_id_with_different_material() -> None:
    session = _session()
    snapshot = _snapshot()
    row = _snapshot_row(snapshot)
    row.phase = ExperimentPhase.DEPLOYMENT.value
    session.get.return_value = row

    with pytest.raises(AuthorizationBindingError):
        SqlAlchemyToolSetSnapshotRepository(session).add(snapshot)

    session.execute.assert_not_called()


def test_plan_authorization_loads_nested_complete_budget() -> None:
    session = _session()
    experiment_id = ExperimentId.new()
    plan_id = PlanId.new()
    session.scalar.return_value = PlanRow(
        id=str(plan_id),
        experiment_id=str(experiment_id),
        kind="benchmark",
        schema_version="plan-envelope/v1",
        body_json={
            "execution_specification": {
                "schema_version": "benchmark-execution-specification/v1",
                "provider": "evalscope",
                "budget": _budget().model_dump(mode="json"),
            }
        },
        plan_hash=str(PLAN_HASH),
        risk_level=RiskLevel.L1.value,
        status=PlanStatus.APPROVED.value,
        approved_by=None,
        created_at=NOW,
    )

    material = SqlAlchemyPlanAuthorizationRepository(session).get_for_execution(
        experiment_id=experiment_id,
        plan_id=plan_id,
        expected_plan_hash=PLAN_HASH,
    )

    assert material.budget == _budget()
    assert material.execution_schema_version == "benchmark-execution-specification/v1"


def test_plan_authorization_rejects_missing_budget_or_unapproved_plan() -> None:
    session = _session()
    session.scalar.return_value = None

    with pytest.raises(PlanAuthorizationError):
        SqlAlchemyPlanAuthorizationRepository(session).get_for_execution(
            experiment_id=ExperimentId.new(),
            plan_id=PlanId.new(),
            expected_plan_hash=PLAN_HASH,
        )


def test_job_authorization_repository_round_trips_worker_evidence() -> None:
    session = _session()
    draft = _draft(_snapshot())
    job_id = JobId.new()
    session.get.return_value = _authorization_row(job_id, draft)

    restored = SqlAlchemyJobAuthorizationRepository(session).get(job_id)

    assert restored == bind_job_authorization(job_id=job_id, draft=draft)


def test_idempotency_gate_locks_before_returning_new_claim() -> None:
    session = _session()
    draft = _draft(_snapshot())
    session.get.return_value = None

    claim = SqlAlchemyJobIdempotencyGate(session).claim(draft)

    assert claim.existing_job_id is None
    sql = str(
        session.execute.call_args.args[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "pg_advisory_xact_lock" in sql


def test_idempotency_gate_replays_only_matching_persisted_authorization() -> None:
    session = _session()
    draft = _draft(_snapshot())
    job_id = JobId.new()
    idempotency = IdempotencyRow(
        idempotency_key=str(draft.idempotency_key),
        schema_version="idempotency-record/v1",
        request_hash=str(draft.request_hash),
        action=str(draft.action),
        experiment_id=str(draft.experiment_id),
        job_id=str(job_id),
        created_at=NOW,
    )
    job = JobRow(id=str(job_id))
    authorization = _authorization_row(job_id, draft)

    def get_row(row_type: type[object], _key: object) -> object | None:
        if row_type is IdempotencyRow:
            return idempotency
        if row_type is JobRow:
            return job
        if row_type is JobAuthorizationRow:
            return authorization
        return None

    session.get.side_effect = get_row

    claim = SqlAlchemyJobIdempotencyGate(session).claim(draft)

    assert claim.existing_job_id == job_id

    idempotency.request_hash = str(Sha256Digest(root=f"sha256:{'f' * 64}"))
    with pytest.raises(IdempotencyAuthorizationError):
        SqlAlchemyJobIdempotencyGate(session).claim(draft)
