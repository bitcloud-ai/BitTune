"""Secret-free contracts exchanged with the mandatory OPA policy boundary."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from autopilot.domain.base import NonEmptyStr, SchemaVersion, StrictModel, UtcDatetime
from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.enums import ApprovalDecision, ExperimentPhase, RiskLevel, UserRole
from autopilot.domain.identifiers import (
    ApprovalId,
    ExperimentId,
    PlanHash,
    PlanId,
    ToolName,
)
from autopilot.domain.identities import HumanSubject, ServiceSubject

INVALID_POLICY_TOOL_CONTRACT = "policy Tool contract values must be unique and ordered"
INVALID_POLICY_APPROVAL_METADATA = "policy approval metadata does not match its decision"
INVALID_POLICY_PLAN_RISK = "policy Plan and Tool risk levels must match"


class PolicyEvaluationPurpose(StrEnum):
    VISIBILITY = "visibility"
    EXECUTION = "execution"


class PolicyReasonCode(StrEnum):
    ALLOW = "ALLOW"
    POLICY_DENIED = "POLICY_DENIED"
    TOOL_CONTEXT_DENIED = "TOOL_CONTEXT_DENIED"
    L3_FORBIDDEN = "L3_FORBIDDEN"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_IDENTITY_DENIED = "APPROVAL_IDENTITY_DENIED"
    APPROVAL_MISMATCH = "APPROVAL_MISMATCH"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"


class PolicyHumanSubject(HumanSubject):
    """Credential-free human identity snapshot sent to OPA."""


class PolicyServiceSubject(ServiceSubject):
    """Credential-free internal service identity snapshot sent to OPA."""


type PolicySubject = PolicyHumanSubject | PolicyServiceSubject


class PolicyTool(StrictModel):
    name: ToolName
    schema_version: SchemaVersion
    risk_level: RiskLevel
    allowed_phases: tuple[ExperimentPhase, ...] = Field(min_length=1)
    allowed_roles: tuple[UserRole, ...] = Field(min_length=1)
    environment_supported: bool
    provider_enabled: bool
    feature_flags_enabled: bool

    @model_validator(mode="after")
    def validate_contract_sets(self) -> Self:
        if (
            tuple(sorted(set(self.allowed_phases), key=str)) != self.allowed_phases
            or tuple(sorted(set(self.allowed_roles), key=str)) != self.allowed_roles
        ):
            raise ValueError(INVALID_POLICY_TOOL_CONTRACT)
        return self


class PolicyPlan(StrictModel):
    experiment_id: ExperimentId
    plan_id: PlanId
    plan_hash: PlanHash
    risk_level: RiskLevel


class PolicyApproval(StrictModel):
    approval_id: ApprovalId
    experiment_id: ExperimentId
    plan_id: PlanId
    plan_hash: PlanHash
    action: ToolName
    decision: ApprovalDecision
    requester: PolicySubject
    decided_by: PolicySubject | None = None
    expires_at: UtcDatetime

    @model_validator(mode="after")
    def validate_decision_metadata(self) -> Self:
        is_decided = self.decision in {
            ApprovalDecision.APPROVED,
            ApprovalDecision.REJECTED,
        }
        if is_decided != (self.decided_by is not None):
            raise ValueError(INVALID_POLICY_APPROVAL_METADATA)
        return self


class PolicyBudget(StrictModel):
    requested: ExecutionBudget
    ceiling: ExecutionBudget


class PolicyInput(StrictModel):
    schema_version: Literal["policy-input/v1"] = "policy-input/v1"
    request_id: NonEmptyStr
    purpose: PolicyEvaluationPurpose
    current_time: UtcDatetime
    phase: ExperimentPhase
    subject: PolicySubject
    tool: PolicyTool
    plan: PolicyPlan | None = None
    approval: PolicyApproval | None = None
    budget: PolicyBudget | None = None

    @model_validator(mode="after")
    def validate_plan_risk(self) -> Self:
        if self.plan is not None and self.plan.risk_level is not self.tool.risk_level:
            raise ValueError(INVALID_POLICY_PLAN_RISK)
        return self


class PolicyRequirements(StrictModel):
    human_approval: bool


class PolicyResult(StrictModel):
    allow: bool
    reason_code: PolicyReasonCode
    requirements: PolicyRequirements


class PolicyDecision(PolicyResult):
    schema_version: Literal["policy-decision/v1"] = "policy-decision/v1"
    decision_id: NonEmptyStr
