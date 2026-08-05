"""Human approval records bound to immutable Plan hashes."""

from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import model_validator

from autopilot.domain.base import LongText, StrictModel, UtcDatetime
from autopilot.domain.enums import ApprovalDecision, RiskLevel
from autopilot.domain.identifiers import ApprovalId, PlanHash, PlanId, ToolName, UserId

INVALID_APPROVAL_EXPIRY = "approval expiry must be later than its request time"
INVALID_APPROVAL_DECISION = "a decided approval requires actor and decision time"
INVALID_APPROVAL_TIMELINE = "approval decision time must be within the approval window"
INVALID_APPROVAL_METADATA = "pending or expired approval cannot contain decision actor metadata"
APPROVAL_NOT_APPROVED = "approval is not approved"
APPROVAL_PLAN_MISMATCH = "approval plan ID does not match the execution request"
APPROVAL_HASH_MISMATCH = "approval plan hash does not match the execution request"
APPROVAL_ACTION_MISMATCH = "approval action does not match the execution request"
APPROVAL_EXPIRED = "approval has expired"
APPROVAL_TIME_REQUIRED = "approval validation requires a timezone-aware timestamp"


class ApprovalRecord(StrictModel):
    schema_version: Literal["approval/v1"] = "approval/v1"
    approval_id: ApprovalId
    plan_id: PlanId
    plan_hash: PlanHash
    action: ToolName
    risk_level: Literal[RiskLevel.L2] = RiskLevel.L2
    requester_id: UserId
    requested_at: UtcDatetime
    expires_at: UtcDatetime
    decision: ApprovalDecision = ApprovalDecision.PENDING
    decided_by: UserId | None = None
    decided_at: UtcDatetime | None = None
    comment: LongText | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        if self.expires_at <= self.requested_at:
            raise ValueError(INVALID_APPROVAL_EXPIRY)
        if self.decision in {ApprovalDecision.APPROVED, ApprovalDecision.REJECTED}:
            if self.decided_by is None or self.decided_at is None:
                raise ValueError(INVALID_APPROVAL_DECISION)
            if not self.requested_at <= self.decided_at <= self.expires_at:
                raise ValueError(INVALID_APPROVAL_TIMELINE)
        if self.decision in {ApprovalDecision.PENDING, ApprovalDecision.EXPIRED} and (
            self.decided_by is not None or self.decided_at is not None
        ):
            raise ValueError(INVALID_APPROVAL_METADATA)
        return self


def validate_approval_for_execution(
    approval: ApprovalRecord,
    expected_plan_id: PlanId,
    expected_plan_hash: PlanHash,
    expected_action: ToolName,
    current_time: datetime,
) -> None:
    """Validate approval status, expiry, and immutable execution binding."""
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError(APPROVAL_TIME_REQUIRED)
    current_time = current_time.astimezone(UTC)
    if approval.decision is not ApprovalDecision.APPROVED:
        raise ValueError(APPROVAL_NOT_APPROVED)
    if approval.plan_id != expected_plan_id:
        raise ValueError(APPROVAL_PLAN_MISMATCH)
    if approval.plan_hash != expected_plan_hash:
        raise ValueError(APPROVAL_HASH_MISMATCH)
    if approval.action != expected_action:
        raise ValueError(APPROVAL_ACTION_MISMATCH)
    if current_time >= approval.expires_at:
        raise ValueError(APPROVAL_EXPIRED)
