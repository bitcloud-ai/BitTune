"""Database-backed human Approval authorization for the Tool Gateway."""

from autopilot.domain.approvals import ApprovalRecord
from autopilot.domain.enums import ApprovalDecision, UserRole
from autopilot.domain.identifiers import (
    ApprovalId,
    ExperimentId,
    PlanHash,
    PlanId,
    ToolName,
)
from autopilot.domain.identities import HumanSubject
from autopilot.gateway.approval_ports import ApprovalRepository
from autopilot.gateway.errors import ApprovalAuthorizationError
from autopilot.infrastructure.database.errors import PersistenceError
from autopilot.policy.models import PolicyApproval, PolicyHumanSubject

APPROVAL_INVALID = "persisted Approval is not valid for this execution"


def _policy_subject(subject: HumanSubject) -> PolicyHumanSubject:
    return PolicyHumanSubject.model_validate(subject.model_dump(mode="json"))


def _policy_approval(record: ApprovalRecord) -> PolicyApproval:
    decided_by = _policy_subject(record.decided_by) if record.decided_by is not None else None
    return PolicyApproval(
        approval_id=record.approval_id,
        experiment_id=record.experiment_id,
        plan_id=record.plan_id,
        plan_hash=record.plan_hash,
        action=record.action,
        decision=record.decision,
        requester=_policy_subject(record.requester),
        decided_by=decided_by,
        expires_at=record.expires_at,
    )


class DatabaseApprovalAuthorizationAdapter:
    """Separate OPA candidate data from authoritative execution revalidation."""

    def __init__(self, repository: ApprovalRepository) -> None:
        self._repository = repository

    def get_candidate(
        self,
        *,
        experiment_id: ExperimentId,
        plan_id: PlanId,
        plan_hash: PlanHash,
        action: ToolName,
    ) -> PolicyApproval | None:
        """Read an Approval candidate for OPA without treating it as authorization."""
        try:
            record = self._repository.get_for_plan(
                experiment_id=experiment_id,
                plan_id=plan_id,
                expected_plan_hash=plan_hash,
                action=action,
            )
        except PersistenceError as error:
            raise ApprovalAuthorizationError(APPROVAL_INVALID) from error
        return _policy_approval(record) if record is not None else None

    def require_valid_for_execution(
        self,
        *,
        approval_id: ApprovalId,
        experiment_id: ExperimentId,
        plan_id: PlanId,
        plan_hash: PlanHash,
        action: ToolName,
    ) -> ApprovalId:
        """Revalidate after OPA using the repository's authoritative database time."""
        try:
            record = self._repository.require_valid_for_execution(
                approval_id=approval_id,
                experiment_id=experiment_id,
                plan_id=plan_id,
                expected_plan_hash=plan_hash,
                action=action,
            )
        except PersistenceError as error:
            raise ApprovalAuthorizationError(APPROVAL_INVALID) from error

        if (
            record.approval_id != approval_id
            or record.experiment_id != experiment_id
            or record.plan_id != plan_id
            or record.plan_hash != plan_hash
            or record.action != action
            or record.decision is not ApprovalDecision.APPROVED
            or record.requester.role not in {UserRole.OPERATOR, UserRole.ADMIN}
            or record.decided_by is None
            or record.decided_by.role is not UserRole.ADMIN
            or record.decided_by.user_id == record.requester.user_id
        ):
            raise ApprovalAuthorizationError(APPROVAL_INVALID)
        return record.approval_id
