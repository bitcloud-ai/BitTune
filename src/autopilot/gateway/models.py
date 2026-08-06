"""Strict Tool Registry, visibility, and authorization snapshot contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, StringConstraints, model_validator

from autopilot.domain.base import NonEmptyStr, SchemaVersion, StrictModel, UtcDatetime
from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.enums import ExperimentPhase, PlanStatus, RiskLevel, UserRole
from autopilot.domain.hashing import compute_content_hash
from autopilot.domain.identifiers import (
    ApprovalId,
    ExperimentId,
    JobId,
    PlanHash,
    PlanId,
    Sha256Digest,
    ToolName,
    ToolSetId,
)
from autopilot.domain.identities import Subject

RegistryName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,127}$"),
]

INVALID_TOOL_REQUIREMENTS = "Tool requirements must not contain duplicate values"
INVALID_TOOL_EXECUTION_MODE = "mutating Tools must use start_* or cancel_* action names"
INVALID_TOOLSET_ENTRIES = "Tool Set entries must be unique and sorted by Tool name"
INVALID_TOOLSET_DECISIONS = "Tool Set policy Decision IDs must be unique and sorted"
TOOLSET_HASH_MISMATCH = "Tool Set version does not match its visibility context"
INVALID_JOB_APPROVAL_BINDING = "L2 Job authorization requires exactly one Approval"
INVALID_JOB_RISK = "L3 actions cannot produce a Job authorization"


class ToolExecutionMode(StrEnum):
    READ_ONLY = "read_only"
    ASYNC_JOB = "async_job"


class ToolDefinition(StrictModel):
    schema_version: Literal["tool-definition/v1"] = "tool-definition/v1"
    name: ToolName
    input_schema_version: SchemaVersion
    risk_level: RiskLevel
    execution_mode: ToolExecutionMode
    allowed_phases: tuple[ExperimentPhase, ...] = Field(min_length=1)
    allowed_roles: tuple[UserRole, ...] = Field(min_length=1)
    required_hardware_capabilities: tuple[RegistryName, ...] = ()
    required_providers: tuple[RegistryName, ...] = ()
    required_feature_flags: tuple[RegistryName, ...] = ()
    requires_plan: bool

    @model_validator(mode="after")
    def validate_requirements(self) -> Self:
        collections = (
            self.allowed_phases,
            self.allowed_roles,
            self.required_hardware_capabilities,
            self.required_providers,
            self.required_feature_flags,
        )
        if any(len(values) != len(set(values)) for values in collections):
            raise ValueError(INVALID_TOOL_REQUIREMENTS)
        if self.execution_mode is ToolExecutionMode.ASYNC_JOB and not str(self.name).startswith(
            ("start_", "cancel_")
        ):
            raise ValueError(INVALID_TOOL_EXECUTION_MODE)
        return self


class GatewayEnvironment(StrictModel):
    schema_version: Literal["gateway-environment/v1"] = "gateway-environment/v1"
    experiment_id: ExperimentId
    subject: Subject
    hardware_capabilities: frozenset[RegistryName] = frozenset()
    enabled_providers: frozenset[RegistryName] = frozenset()
    enabled_feature_flags: frozenset[RegistryName] = frozenset()


class VisibilityContext(StrictModel):
    schema_version: Literal["visibility-context/v1"] = "visibility-context/v1"
    experiment_id: ExperimentId
    subject: Subject
    phase: ExperimentPhase
    hardware_capabilities: frozenset[RegistryName] = frozenset()
    enabled_providers: frozenset[RegistryName] = frozenset()
    enabled_feature_flags: frozenset[RegistryName] = frozenset()


class ToolSetEntry(StrictModel):
    name: ToolName
    schema_version: SchemaVersion
    risk_level: RiskLevel


class ToolSetHashMaterial(StrictModel):
    schema_version: Literal["tool-set-hash-material/v1"] = "tool-set-hash-material/v1"
    experiment_id: ExperimentId
    subject: Subject
    phase: ExperimentPhase
    hardware_capabilities: tuple[RegistryName, ...]
    enabled_providers: tuple[RegistryName, ...]
    enabled_feature_flags: tuple[RegistryName, ...]
    tools: tuple[ToolSetEntry, ...]


def toolset_hash_material(
    context: VisibilityContext,
    tools: tuple[ToolSetEntry, ...],
) -> ToolSetHashMaterial:
    return ToolSetHashMaterial(
        experiment_id=context.experiment_id,
        subject=context.subject,
        phase=context.phase,
        hardware_capabilities=tuple(sorted(context.hardware_capabilities)),
        enabled_providers=tuple(sorted(context.enabled_providers)),
        enabled_feature_flags=tuple(sorted(context.enabled_feature_flags)),
        tools=tools,
    )


class ToolSetSnapshot(StrictModel):
    schema_version: Literal["tool-set-snapshot/v1"] = "tool-set-snapshot/v1"
    tool_set_id: ToolSetId
    tool_set_version: Sha256Digest
    experiment_id: ExperimentId
    subject: Subject
    phase: ExperimentPhase
    hardware_capabilities: tuple[RegistryName, ...]
    enabled_providers: tuple[RegistryName, ...]
    enabled_feature_flags: tuple[RegistryName, ...]
    tools: tuple[ToolSetEntry, ...]
    policy_decision_ids: tuple[NonEmptyStr, ...]
    created_at: UtcDatetime

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        names = tuple(str(entry.name) for entry in self.tools)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError(INVALID_TOOLSET_ENTRIES)
        if self.policy_decision_ids != tuple(sorted(set(self.policy_decision_ids))):
            raise ValueError(INVALID_TOOLSET_DECISIONS)
        context = VisibilityContext(
            experiment_id=self.experiment_id,
            subject=self.subject,
            phase=self.phase,
            hardware_capabilities=frozenset(self.hardware_capabilities),
            enabled_providers=frozenset(self.enabled_providers),
            enabled_feature_flags=frozenset(self.enabled_feature_flags),
        )
        expected = compute_content_hash(toolset_hash_material(context, self.tools))
        if expected != self.tool_set_version:
            raise ValueError(TOOLSET_HASH_MISMATCH)
        return self


def create_toolset_snapshot(
    *,
    context: VisibilityContext,
    tools: tuple[ToolSetEntry, ...],
    policy_decision_ids: tuple[str, ...],
    created_at: UtcDatetime,
) -> ToolSetSnapshot:
    ordered_tools = tuple(sorted(tools, key=lambda entry: str(entry.name)))
    return ToolSetSnapshot(
        tool_set_id=ToolSetId.new(),
        tool_set_version=compute_content_hash(toolset_hash_material(context, ordered_tools)),
        experiment_id=context.experiment_id,
        subject=context.subject,
        phase=context.phase,
        hardware_capabilities=tuple(sorted(context.hardware_capabilities)),
        enabled_providers=tuple(sorted(context.enabled_providers)),
        enabled_feature_flags=tuple(sorted(context.enabled_feature_flags)),
        tools=ordered_tools,
        policy_decision_ids=tuple(sorted(set(policy_decision_ids))),
        created_at=created_at,
    )


class ToolCallRequest(StrictModel):
    schema_version: Literal["tool-call-request/v1"] = "tool-call-request/v1"
    request_id: NonEmptyStr
    tool_name: ToolName
    tool_set_id: ToolSetId
    expected_tool_set_version: Sha256Digest
    arguments: dict[NonEmptyStr, JsonValue] = Field(max_length=128)


class PlanAuthorizationMaterial(StrictModel):
    schema_version: Literal["plan-authorization-material/v1"] = "plan-authorization-material/v1"
    experiment_id: ExperimentId
    plan_id: PlanId
    plan_hash: PlanHash
    status: PlanStatus
    risk_level: RiskLevel
    execution_schema_version: SchemaVersion
    budget: ExecutionBudget


class _JobAuthorizationBase(StrictModel):
    experiment_id: ExperimentId
    subject: Subject
    action: ToolName
    risk_level: RiskLevel
    plan_id: PlanId
    plan_hash: PlanHash
    approval_id: ApprovalId | None = None
    tool_schema_version: SchemaVersion
    tool_set_id: ToolSetId
    tool_set_version: Sha256Digest
    policy_decision_id: NonEmptyStr
    request_hash: Sha256Digest
    idempotency_key: Sha256Digest
    authorized_at: UtcDatetime

    @model_validator(mode="after")
    def validate_approval_binding(self) -> Self:
        if self.risk_level is RiskLevel.L3:
            raise ValueError(INVALID_JOB_RISK)
        if (self.risk_level is RiskLevel.L2) != (self.approval_id is not None):
            raise ValueError(INVALID_JOB_APPROVAL_BINDING)
        return self


class JobAuthorizationDraft(_JobAuthorizationBase):
    schema_version: Literal["job-authorization-draft/v1"] = "job-authorization-draft/v1"


class JobAuthorizationRecord(_JobAuthorizationBase):
    """Immutable authorization evidence bound one-to-one to a persisted Job."""

    schema_version: Literal["job-authorization/v1"] = "job-authorization/v1"
    job_id: JobId


def bind_job_authorization(
    *,
    job_id: JobId,
    draft: JobAuthorizationDraft,
) -> JobAuthorizationRecord:
    return JobAuthorizationRecord(
        **draft.model_dump(exclude={"schema_version"}),
        job_id=job_id,
    )


class JobIdempotencyClaim(StrictModel):
    """Result of the transaction-scoped idempotency gate before resource reservation."""

    schema_version: Literal["job-idempotency-claim/v1"] = "job-idempotency-claim/v1"
    idempotency_key: Sha256Digest
    existing_job_id: JobId | None = None


class AuthorizedReadOnlyCall(StrictModel):
    schema_version: Literal["authorized-read-only-call/v1"] = "authorized-read-only-call/v1"
    experiment_id: ExperimentId
    subject: Subject
    action: ToolName
    tool_schema_version: SchemaVersion
    tool_set_id: ToolSetId
    tool_set_version: Sha256Digest
    policy_decision_id: NonEmptyStr
    request_hash: Sha256Digest
    authorized_at: UtcDatetime


class AuthorizedJobControl(StrictModel):
    """Authorization evidence for idempotent control of an existing Job."""

    schema_version: Literal["authorized-job-control/v1"] = "authorized-job-control/v1"
    experiment_id: ExperimentId
    subject: Subject
    action: ToolName
    tool_schema_version: SchemaVersion
    tool_set_id: ToolSetId
    tool_set_version: Sha256Digest
    policy_decision_id: NonEmptyStr
    request_hash: Sha256Digest
    authorized_at: UtcDatetime


class IdempotencyKeyMaterial(StrictModel):
    schema_version: Literal["idempotency-key-material/v1"] = "idempotency-key-material/v1"
    experiment_id: ExperimentId
    action: ToolName
    tool_schema_version: SchemaVersion
    request_hash: Sha256Digest
