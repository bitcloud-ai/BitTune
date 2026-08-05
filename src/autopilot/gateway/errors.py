"""Typed, provider-safe Tool Gateway failures."""

from enum import StrEnum

from autopilot.domain.base import NonEmptyStr


class GatewayErrorCode(StrEnum):
    TOOL_NOT_REGISTERED = "TOOL_NOT_REGISTERED"
    TOOL_NOT_VISIBLE = "TOOL_NOT_VISIBLE"
    TOOL_SET_NOT_FOUND = "TOOL_SET_NOT_FOUND"
    TOOL_SET_MISMATCH = "TOOL_SET_MISMATCH"
    SCHEMA_REJECTED = "SCHEMA_REJECTED"
    WORKFLOW_STATE_REJECTED = "WORKFLOW_STATE_REJECTED"
    PLAN_REJECTED = "PLAN_REJECTED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    POLICY_UNAVAILABLE = "POLICY_UNAVAILABLE"
    POLICY_DENIED = "POLICY_DENIED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    RESOURCE_UNAVAILABLE = "RESOURCE_UNAVAILABLE"
    DISPATCH_FAILED = "DISPATCH_FAILED"


class ToolGatewayError(RuntimeError):
    """A classified Gateway failure that contains no raw input or Provider trace."""

    def __init__(self, code: GatewayErrorCode, message: NonEmptyStr) -> None:
        self.code = code
        super().__init__(message)


class WorkflowStateError(RuntimeError):
    """Persisted Graph state could not authorize the requested phase."""


class PlanAuthorizationError(RuntimeError):
    """The persisted Plan is missing, invalid, or no longer executable."""


class ApprovalAuthorizationError(RuntimeError):
    """The persisted Approval is missing, expired, or does not match execution."""


class IdempotencyAuthorizationError(RuntimeError):
    """An idempotency key is bound to different immutable request material."""


class ResourceUnavailableError(RuntimeError):
    """The required resource cannot be reserved for this authorized request."""


class ToolDispatchError(RuntimeError):
    """A registered capability service rejected a dispatch request."""
