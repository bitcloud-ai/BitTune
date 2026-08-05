from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from autopilot.domain.approvals import (
    ApprovalExecutionBinding,
    ApprovalRecord,
    validate_approval_for_execution,
)
from autopilot.domain.enums import ApprovalDecision, UserRole
from autopilot.domain.identifiers import (
    ApprovalId,
    ExperimentId,
    PlanHash,
    PlanId,
    ToolName,
    UserId,
)
from autopilot.domain.identities import HumanSubject
from autopilot.gateway.approval_ports import ApprovalRepository
from autopilot.gateway.errors import ApprovalAuthorizationError
from autopilot.infrastructure.database.approval_authorization import (
    APPROVAL_INVALID,
    DatabaseApprovalAuthorizationAdapter,
)
from autopilot.infrastructure.database.errors import (
    ApprovalActorConflictError,
    ApprovalNotFoundError,
    ApprovalStateConflictError,
)
from autopilot.policy.models import PolicyHumanSubject

NOW = datetime(2026, 8, 6, 8, 0, tzinfo=UTC)
PLAN_HASH = PlanHash(root=f"sha256:{'a' * 64}")
ACTION = ToolName(root="start_benchmark")
APPROVAL_DOES_NOT_EXIST = "Approval does not exist"
APPROVAL_NOT_ACTIVE = "Approval is not active"
APPROVAL_ACTOR_INVALID = "Approval actor is invalid"


def _human(role: UserRole) -> HumanSubject:
    return HumanSubject(user_id=UserId.new(), role=role)


def _approved_record(
    *,
    experiment_id: ExperimentId | None = None,
    plan_id: PlanId | None = None,
    plan_hash: PlanHash = PLAN_HASH,
    action: ToolName = ACTION,
    expires_at: datetime | None = None,
) -> ApprovalRecord:
    requester = _human(UserRole.OPERATOR)
    return ApprovalRecord(
        approval_id=ApprovalId.new(),
        experiment_id=experiment_id or ExperimentId.new(),
        plan_id=plan_id or PlanId.new(),
        plan_hash=plan_hash,
        action=action,
        requester=requester,
        requested_at=NOW - timedelta(minutes=5),
        expires_at=expires_at or NOW + timedelta(minutes=5),
        decision=ApprovalDecision.APPROVED,
        decided_by=_human(UserRole.ADMIN),
        decided_at=NOW - timedelta(minutes=4),
    )


class FakeAuthoritativeApprovalRepository:
    """Use a fixed authoritative time to model the PostgreSQL validation boundary."""

    def __init__(self, record: ApprovalRecord | None, *, current_time: datetime = NOW) -> None:
        self.record = record
        self.current_time = current_time
        self.candidate_reads = 0
        self.execution_checks = 0

    def get_for_plan(
        self,
        *,
        experiment_id: ExperimentId,
        plan_id: PlanId,
        expected_plan_hash: PlanHash,
        action: ToolName,
    ) -> ApprovalRecord | None:
        self.candidate_reads += 1
        record = self.record
        if record is None:
            return None
        if (
            record.experiment_id != experiment_id
            or record.plan_id != plan_id
            or record.plan_hash != expected_plan_hash
            or record.action != action
        ):
            return None
        return record

    def require_valid_for_execution(
        self,
        *,
        approval_id: ApprovalId,
        experiment_id: ExperimentId,
        plan_id: PlanId,
        expected_plan_hash: PlanHash,
        action: ToolName,
    ) -> ApprovalRecord:
        self.execution_checks += 1
        record = self.record
        if record is None or record.approval_id != approval_id:
            raise ApprovalNotFoundError(APPROVAL_DOES_NOT_EXIST)
        try:
            validate_approval_for_execution(
                record,
                ApprovalExecutionBinding(
                    experiment_id=experiment_id,
                    plan_id=plan_id,
                    plan_hash=expected_plan_hash,
                    action=action,
                ),
                self.current_time,
            )
        except ValueError as error:
            raise ApprovalStateConflictError(APPROVAL_NOT_ACTIVE) from error
        return record


def _adapter(
    repository: FakeAuthoritativeApprovalRepository,
) -> DatabaseApprovalAuthorizationAdapter:
    return DatabaseApprovalAuthorizationAdapter(cast(ApprovalRepository, repository))


def test_get_candidate_only_reads_and_converts_policy_data() -> None:
    record = _approved_record(expires_at=NOW - timedelta(seconds=1))
    repository = FakeAuthoritativeApprovalRepository(record)

    candidate = _adapter(repository).get_candidate(
        experiment_id=record.experiment_id,
        plan_id=record.plan_id,
        plan_hash=record.plan_hash,
        action=record.action,
    )

    assert candidate is not None
    assert candidate.approval_id == record.approval_id
    assert isinstance(candidate.requester, PolicyHumanSubject)
    assert isinstance(candidate.decided_by, PolicyHumanSubject)
    assert candidate.expires_at < repository.current_time
    assert repository.candidate_reads == 1
    assert repository.execution_checks == 0


def test_get_candidate_returns_none_when_no_matching_approval_exists() -> None:
    repository = FakeAuthoritativeApprovalRepository(None)

    candidate = _adapter(repository).get_candidate(
        experiment_id=ExperimentId.new(),
        plan_id=PlanId.new(),
        plan_hash=PLAN_HASH,
        action=ACTION,
    )

    assert candidate is None
    assert repository.candidate_reads == 1
    assert repository.execution_checks == 0


def test_get_candidate_maps_persistence_failure() -> None:
    repository = FakeAuthoritativeApprovalRepository(None)

    def fail_read(**_kwargs: object) -> ApprovalRecord | None:
        raise ApprovalStateConflictError(APPROVAL_NOT_ACTIVE)

    repository.get_for_plan = fail_read  # type: ignore[method-assign]

    with pytest.raises(ApprovalAuthorizationError, match=APPROVAL_INVALID):
        _adapter(repository).get_candidate(
            experiment_id=ExperimentId.new(),
            plan_id=PlanId.new(),
            plan_hash=PLAN_HASH,
            action=ACTION,
        )


def test_require_valid_for_execution_accepts_authoritative_approval() -> None:
    record = _approved_record()
    repository = FakeAuthoritativeApprovalRepository(record)

    approval_id = _adapter(repository).require_valid_for_execution(
        approval_id=record.approval_id,
        experiment_id=record.experiment_id,
        plan_id=record.plan_id,
        plan_hash=record.plan_hash,
        action=record.action,
    )

    assert approval_id == record.approval_id
    assert repository.candidate_reads == 0
    assert repository.execution_checks == 1


def test_require_valid_for_execution_rejects_missing_approval() -> None:
    repository = FakeAuthoritativeApprovalRepository(None)

    with pytest.raises(ApprovalAuthorizationError, match=APPROVAL_INVALID):
        _adapter(repository).require_valid_for_execution(
            approval_id=ApprovalId.new(),
            experiment_id=ExperimentId.new(),
            plan_id=PlanId.new(),
            plan_hash=PLAN_HASH,
            action=ACTION,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("experiment_id", ExperimentId.new()),
        ("plan_id", PlanId.new()),
        ("plan_hash", PlanHash(root=f"sha256:{'b' * 64}")),
        ("action", ToolName(root="start_deployment")),
    ],
)
def test_require_valid_for_execution_rejects_binding_mismatch(
    field: str,
    value: object,
) -> None:
    record = _approved_record()
    arguments = {
        "approval_id": record.approval_id,
        "experiment_id": record.experiment_id,
        "plan_id": record.plan_id,
        "plan_hash": record.plan_hash,
        "action": record.action,
    }
    arguments[field] = value

    with pytest.raises(ApprovalAuthorizationError, match=APPROVAL_INVALID):
        _adapter(FakeAuthoritativeApprovalRepository(record)).require_valid_for_execution(
            **arguments
        )


def test_require_valid_for_execution_uses_authoritative_expiry_time() -> None:
    record = _approved_record(expires_at=NOW + timedelta(seconds=1))
    repository = FakeAuthoritativeApprovalRepository(
        record,
        current_time=NOW + timedelta(seconds=2),
    )

    with pytest.raises(ApprovalAuthorizationError, match=APPROVAL_INVALID):
        _adapter(repository).require_valid_for_execution(
            approval_id=record.approval_id,
            experiment_id=record.experiment_id,
            plan_id=record.plan_id,
            plan_hash=record.plan_hash,
            action=record.action,
        )


def test_require_valid_for_execution_maps_identity_rejection() -> None:
    record = _approved_record()
    repository = FakeAuthoritativeApprovalRepository(record)

    def reject_identity(**_kwargs: object) -> ApprovalRecord:
        raise ApprovalActorConflictError(APPROVAL_ACTOR_INVALID)

    repository.require_valid_for_execution = reject_identity  # type: ignore[method-assign]

    with pytest.raises(ApprovalAuthorizationError, match=APPROVAL_INVALID):
        _adapter(repository).require_valid_for_execution(
            approval_id=record.approval_id,
            experiment_id=record.experiment_id,
            plan_id=record.plan_id,
            plan_hash=record.plan_hash,
            action=record.action,
        )
