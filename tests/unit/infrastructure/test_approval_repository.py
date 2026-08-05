from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from autopilot.domain.enums import ApprovalDecision, PlanStatus, UserRole
from autopilot.domain.identifiers import (
    ApprovalId,
    ExperimentId,
    PlanHash,
    PlanId,
    ToolName,
    UserId,
)
from autopilot.domain.identities import HumanSubject
from autopilot.gateway.approval_ports import CreateApprovalRequest, DecideApprovalRequest
from autopilot.infrastructure.database.errors import (
    ApprovalActorConflictError,
    ApprovalBindingError,
    ApprovalStateConflictError,
)
from autopilot.infrastructure.database.models import ApprovalRow, PlanRow
from autopilot.infrastructure.database.repositories import SqlAlchemyApprovalRepository

_REQUESTED_AT = datetime(2026, 8, 6, 8, 30, tzinfo=UTC)


def _plan(
    *,
    experiment_id: ExperimentId,
    plan_id: PlanId,
    plan_hash: PlanHash,
    status: PlanStatus = PlanStatus.DRAFT,
    risk_level: str = "L2",
) -> PlanRow:
    return PlanRow(
        id=str(plan_id),
        experiment_id=str(experiment_id),
        kind="deployment",
        schema_version="deployment-plan/v1",
        body_json={},
        plan_hash=str(plan_hash),
        risk_level=risk_level,
        status=status.value,
        approved_by=None,
        created_at=datetime.now(UTC),
    )


def _approval(
    *,
    approval_id: ApprovalId,
    experiment_id: ExperimentId,
    plan_id: PlanId,
    plan_hash: PlanHash,
    requester: HumanSubject,
) -> ApprovalRow:
    return ApprovalRow(
        id=str(approval_id),
        schema_version="approval/v2",
        experiment_id=str(experiment_id),
        plan_id=str(plan_id),
        plan_hash=str(plan_hash),
        action="start_deployment",
        risk_level="L2",
        requester_kind=requester.kind.value,
        requester_id=str(requester.user_id),
        requester_role=requester.role.value,
        requested_at=_REQUESTED_AT,
        expires_at=_REQUESTED_AT + timedelta(minutes=15),
        decision="pending",
        decided_by_kind=None,
        decided_by_id=None,
        decided_by_role=None,
        decided_at=None,
        comment=None,
    )


def _session() -> MagicMock:
    return MagicMock(spec=Session)


def _human(*, role: UserRole, user_id: UserId | None = None) -> HumanSubject:
    return HumanSubject(user_id=user_id or UserId.new(), role=role)


def test_create_uses_database_time_and_leaves_commit_to_caller() -> None:
    session = _session()
    experiment_id = ExperimentId.new()
    plan_id = PlanId.new()
    plan_hash = PlanHash(root=f"sha256:{'1' * 64}")
    approval_id = ApprovalId.new()
    now = datetime(2026, 8, 6, 8, 30, tzinfo=UTC)
    requester = _human(role=UserRole.OPERATOR)
    session.scalar.side_effect = [
        _plan(experiment_id=experiment_id, plan_id=plan_id, plan_hash=plan_hash),
        None,
        now,
    ]
    session.execute.return_value.scalar_one_or_none.return_value = str(approval_id)

    result = SqlAlchemyApprovalRepository(session).create(
        CreateApprovalRequest(
            approval_id=approval_id,
            experiment_id=experiment_id,
            plan_id=plan_id,
            expected_plan_hash=plan_hash,
            action=ToolName(root="start_deployment"),
            requester=requester,
            expires_in=timedelta(minutes=15),
            comment="  deploy approved image  ",
        )
    )

    assert result.requested_at == now
    assert result.expires_at == now + timedelta(minutes=15)
    assert result.experiment_id == experiment_id
    assert result.requester == requester
    assert result.comment == "deploy approved image"
    session.flush.assert_called_once_with()
    session.commit.assert_not_called()


def test_create_rejects_plan_hash_or_risk_mismatch() -> None:
    session = _session()
    experiment_id = ExperimentId.new()
    plan_id = PlanId.new()
    stored_hash = PlanHash(root=f"sha256:{'2' * 64}")
    expected_hash = PlanHash(root=f"sha256:{'3' * 64}")
    session.scalar.return_value = _plan(
        experiment_id=experiment_id,
        plan_id=plan_id,
        plan_hash=stored_hash,
    )

    with pytest.raises(ApprovalBindingError, match="immutable L2 Plan"):
        SqlAlchemyApprovalRepository(session).create(
            CreateApprovalRequest(
                approval_id=ApprovalId.new(),
                experiment_id=experiment_id,
                plan_id=plan_id,
                expected_plan_hash=expected_hash,
                action=ToolName(root="start_deployment"),
                requester=_human(role=UserRole.OPERATOR),
                expires_in=timedelta(minutes=15),
            )
        )

    session.execute.assert_not_called()
    session.commit.assert_not_called()


def test_decide_rejects_self_approval_before_mutating_rows() -> None:
    session = _session()
    experiment_id = ExperimentId.new()
    plan_id = PlanId.new()
    plan_hash = PlanHash(root=f"sha256:{'4' * 64}")
    approval_id = ApprovalId.new()
    requester = _human(role=UserRole.OPERATOR)
    row = _approval(
        approval_id=approval_id,
        experiment_id=experiment_id,
        plan_id=plan_id,
        plan_hash=plan_hash,
        requester=requester,
    )
    session.scalar.side_effect = [
        _plan(experiment_id=experiment_id, plan_id=plan_id, plan_hash=plan_hash),
        row,
    ]

    with pytest.raises(ApprovalActorConflictError, match="own Approval"):
        SqlAlchemyApprovalRepository(session).decide(
            DecideApprovalRequest(
                approval_id=approval_id,
                experiment_id=experiment_id,
                expected_plan_id=plan_id,
                expected_plan_hash=plan_hash,
                expected_action=ToolName(root="start_deployment"),
                actor=_human(role=UserRole.ADMIN, user_id=requester.user_id),
                decision=ApprovalDecision.APPROVED,
            )
        )

    assert row.decision == ApprovalDecision.PENDING.value
    session.flush.assert_not_called()
    session.commit.assert_not_called()


def test_decide_updates_approval_and_plan_in_one_caller_owned_transaction() -> None:
    session = _session()
    experiment_id = ExperimentId.new()
    plan_id = PlanId.new()
    plan_hash = PlanHash(root=f"sha256:{'5' * 64}")
    approval_id = ApprovalId.new()
    requester = _human(role=UserRole.OPERATOR)
    actor = _human(role=UserRole.ADMIN)
    requested_at = datetime(2026, 8, 6, 8, 30, tzinfo=UTC)
    decided_at = requested_at + timedelta(minutes=1)
    row = _approval(
        approval_id=approval_id,
        experiment_id=experiment_id,
        plan_id=plan_id,
        plan_hash=plan_hash,
        requester=requester,
    )
    plan = _plan(experiment_id=experiment_id, plan_id=plan_id, plan_hash=plan_hash)
    session.scalar.side_effect = [plan, row, decided_at]

    result = SqlAlchemyApprovalRepository(session).decide(
        DecideApprovalRequest(
            approval_id=approval_id,
            experiment_id=experiment_id,
            expected_plan_id=plan_id,
            expected_plan_hash=plan_hash,
            expected_action=ToolName(root="start_deployment"),
            actor=actor,
            decision=ApprovalDecision.APPROVED,
            comment="approved for the bounded deployment",
        )
    )

    assert result.decision is ApprovalDecision.APPROVED
    assert result.decided_by == actor
    assert result.decided_at == decided_at
    assert row.decision == ApprovalDecision.APPROVED.value
    assert plan.status == PlanStatus.APPROVED.value
    assert plan.approved_by == str(actor.user_id)
    session.flush.assert_called_once_with()
    session.commit.assert_not_called()


def test_decide_persists_expiry_without_changing_plan_status() -> None:
    session = _session()
    experiment_id = ExperimentId.new()
    plan_id = PlanId.new()
    plan_hash = PlanHash(root=f"sha256:{'6' * 64}")
    approval_id = ApprovalId.new()
    requested_at = datetime(2026, 8, 6, 8, 30, tzinfo=UTC)
    row = _approval(
        approval_id=approval_id,
        experiment_id=experiment_id,
        plan_id=plan_id,
        plan_hash=plan_hash,
        requester=_human(role=UserRole.OPERATOR),
    )
    row.expires_at = requested_at + timedelta(minutes=1)
    plan = _plan(experiment_id=experiment_id, plan_id=plan_id, plan_hash=plan_hash)
    session.scalar.side_effect = [plan, row, requested_at + timedelta(minutes=2)]

    result = SqlAlchemyApprovalRepository(session).decide(
        DecideApprovalRequest(
            approval_id=approval_id,
            experiment_id=experiment_id,
            expected_plan_id=plan_id,
            expected_plan_hash=plan_hash,
            expected_action=ToolName(root="start_deployment"),
            actor=_human(role=UserRole.ADMIN),
            decision=ApprovalDecision.APPROVED,
        )
    )

    assert result.decision is ApprovalDecision.EXPIRED
    assert row.decision == ApprovalDecision.EXPIRED.value
    assert plan.status == PlanStatus.DRAFT.value
    assert plan.approved_by is None
    session.flush.assert_called_once_with()
    session.commit.assert_not_called()


def test_require_valid_for_execution_uses_database_time_and_full_binding() -> None:
    session = _session()
    experiment_id = ExperimentId.new()
    plan_id = PlanId.new()
    plan_hash = PlanHash(root=f"sha256:{'7' * 64}")
    approval_id = ApprovalId.new()
    requested_at = datetime(2026, 8, 6, 8, 30, tzinfo=UTC)
    actor = _human(role=UserRole.ADMIN)
    row = _approval(
        approval_id=approval_id,
        experiment_id=experiment_id,
        plan_id=plan_id,
        plan_hash=plan_hash,
        requester=_human(role=UserRole.OPERATOR),
    )
    row.decision = ApprovalDecision.APPROVED.value
    row.decided_by_kind = actor.kind.value
    row.decided_by_id = str(actor.user_id)
    row.decided_by_role = actor.role.value
    row.decided_at = requested_at + timedelta(minutes=1)
    database_now = requested_at + timedelta(minutes=2)
    session.scalar.side_effect = [row, database_now]

    result = SqlAlchemyApprovalRepository(session).require_valid_for_execution(
        approval_id=approval_id,
        experiment_id=experiment_id,
        plan_id=plan_id,
        expected_plan_hash=plan_hash,
        action=ToolName(root="start_deployment"),
    )

    assert result.decided_by == actor
    assert result.experiment_id == experiment_id
    assert session.scalar.call_count == 2


def test_require_valid_for_execution_rejects_expired_approval() -> None:
    session = _session()
    experiment_id = ExperimentId.new()
    plan_id = PlanId.new()
    plan_hash = PlanHash(root=f"sha256:{'8' * 64}")
    approval_id = ApprovalId.new()
    requested_at = datetime(2026, 8, 6, 8, 30, tzinfo=UTC)
    actor = _human(role=UserRole.ADMIN)
    row = _approval(
        approval_id=approval_id,
        experiment_id=experiment_id,
        plan_id=plan_id,
        plan_hash=plan_hash,
        requester=_human(role=UserRole.OPERATOR),
    )
    row.decision = ApprovalDecision.APPROVED.value
    row.decided_by_kind = actor.kind.value
    row.decided_by_id = str(actor.user_id)
    row.decided_by_role = actor.role.value
    row.decided_at = requested_at + timedelta(minutes=1)
    session.scalar.side_effect = [row, row.expires_at]

    with pytest.raises(ApprovalStateConflictError, match="not active"):
        SqlAlchemyApprovalRepository(session).require_valid_for_execution(
            approval_id=approval_id,
            experiment_id=experiment_id,
            plan_id=plan_id,
            expected_plan_hash=plan_hash,
            action=ToolName(root="start_deployment"),
        )


@pytest.mark.parametrize("mismatch", ["experiment", "plan", "hash", "action"])
def test_require_valid_for_execution_rejects_each_binding_mismatch(mismatch: str) -> None:
    session = _session()
    experiment_id = ExperimentId.new()
    plan_id = PlanId.new()
    plan_hash = PlanHash(root=f"sha256:{'9' * 64}")
    row = _approval(
        approval_id=ApprovalId.new(),
        experiment_id=experiment_id,
        plan_id=plan_id,
        plan_hash=plan_hash,
        requester=_human(role=UserRole.OPERATOR),
    )
    session.scalar.return_value = row
    supplied_experiment_id = ExperimentId.new() if mismatch == "experiment" else experiment_id
    supplied_plan_id = PlanId.new() if mismatch == "plan" else plan_id
    supplied_plan_hash = PlanHash(root=f"sha256:{'a' * 64}") if mismatch == "hash" else plan_hash
    supplied_action = (
        ToolName(root="start_benchmark")
        if mismatch == "action"
        else ToolName(root="start_deployment")
    )

    with pytest.raises(ApprovalBindingError, match="immutable L2 Plan"):
        SqlAlchemyApprovalRepository(session).require_valid_for_execution(
            approval_id=ApprovalId(root=row.id),
            experiment_id=supplied_experiment_id,
            plan_id=supplied_plan_id,
            expected_plan_hash=supplied_plan_hash,
            action=supplied_action,
        )

    assert session.scalar.call_count == 1
