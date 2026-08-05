"""Application-facing persistence Port for independent human L2 approvals."""

from datetime import timedelta
from typing import Literal, Protocol, Self

from pydantic import model_validator

from autopilot.domain.approvals import ApprovalRecord
from autopilot.domain.base import LongText, StrictModel
from autopilot.domain.enums import ApprovalDecision, UserRole
from autopilot.domain.identifiers import ApprovalId, ExperimentId, PlanHash, PlanId, ToolName
from autopilot.domain.identities import HumanSubject

INVALID_APPROVAL_REQUESTER = "L2 Approval requests require a human operator or admin"
INVALID_APPROVAL_DECIDER = "Approval decisions require a human admin"
INVALID_APPROVAL_DECISION = "Approval decision must be approved or rejected"
INVALID_APPROVAL_EXPIRY = "Approval expiry duration must be positive"


class CreateApprovalRequest(StrictModel):
    schema_version: Literal["create-approval-request/v1"] = "create-approval-request/v1"
    approval_id: ApprovalId
    experiment_id: ExperimentId
    plan_id: PlanId
    expected_plan_hash: PlanHash
    action: ToolName
    requester: HumanSubject
    expires_in: timedelta
    comment: LongText | None = None

    @model_validator(mode="after")
    def validate_requester(self) -> Self:
        if self.requester.role not in {UserRole.OPERATOR, UserRole.ADMIN}:
            raise ValueError(INVALID_APPROVAL_REQUESTER)
        if self.expires_in <= timedelta(0):
            raise ValueError(INVALID_APPROVAL_EXPIRY)
        return self


class DecideApprovalRequest(StrictModel):
    schema_version: Literal["decide-approval-request/v1"] = "decide-approval-request/v1"
    approval_id: ApprovalId
    experiment_id: ExperimentId
    expected_plan_id: PlanId
    expected_plan_hash: PlanHash
    expected_action: ToolName
    actor: HumanSubject
    decision: ApprovalDecision
    comment: LongText | None = None

    @model_validator(mode="after")
    def validate_decider(self) -> Self:
        if self.actor.role is not UserRole.ADMIN:
            raise ValueError(INVALID_APPROVAL_DECIDER)
        if self.decision not in {ApprovalDecision.APPROVED, ApprovalDecision.REJECTED}:
            raise ValueError(INVALID_APPROVAL_DECISION)
        return self


class ApprovalRepository(Protocol):
    """Persist approval lifecycle changes in a caller-owned transaction."""

    def create(self, request: CreateApprovalRequest) -> ApprovalRecord: ...

    def get(self, approval_id: ApprovalId) -> ApprovalRecord | None: ...

    def get_for_plan(
        self,
        *,
        experiment_id: ExperimentId,
        plan_id: PlanId,
        expected_plan_hash: PlanHash,
        action: ToolName,
    ) -> ApprovalRecord | None: ...

    def require_valid_for_execution(
        self,
        *,
        approval_id: ApprovalId,
        experiment_id: ExperimentId,
        plan_id: PlanId,
        expected_plan_hash: PlanHash,
        action: ToolName,
    ) -> ApprovalRecord: ...

    def decide(self, request: DecideApprovalRequest) -> ApprovalRecord: ...
