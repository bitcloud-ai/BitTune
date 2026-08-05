"""Fixed runtime Tool Registry and six-dimensional visibility resolver."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from pydantic import BaseModel

from autopilot.domain.enums import RiskLevel
from autopilot.domain.identifiers import ToolName
from autopilot.domain.identities import HumanSubject, ServiceSubject
from autopilot.domain.plans import PlanExecutionRequest
from autopilot.gateway.models import (
    ToolDefinition,
    ToolSetEntry,
    ToolSetSnapshot,
    VisibilityContext,
    create_toolset_snapshot,
)
from autopilot.policy.models import (
    PolicyEvaluationPurpose,
    PolicyHumanSubject,
    PolicyInput,
    PolicyServiceSubject,
    PolicyTool,
)
from autopilot.policy.ports import PolicyClient

DUPLICATE_TOOL: Final = "Tool Registry contains a duplicate Tool name"
TOOL_SCHEMA_MISMATCH: Final = "registered input model Schema Version does not match the Tool"
TOOL_SCHEMA_REQUIRED: Final = "registered input model must declare a static Schema Version"


@dataclass(frozen=True, slots=True)
class ToolRegistration:
    definition: ToolDefinition
    input_model: type[BaseModel]

    def __post_init__(self) -> None:
        schema_field = self.input_model.model_fields.get("schema_version")
        if schema_field is None or not isinstance(schema_field.default, str):
            raise ValueError(TOOL_SCHEMA_REQUIRED)
        if schema_field.default != self.definition.input_schema_version:
            raise ValueError(TOOL_SCHEMA_MISMATCH)
        if self.definition.requires_plan and not issubclass(self.input_model, PlanExecutionRequest):
            raise ValueError(TOOL_SCHEMA_MISMATCH)


def _policy_subject(
    subject: HumanSubject | ServiceSubject,
) -> PolicyHumanSubject | PolicyServiceSubject:
    if isinstance(subject, HumanSubject):
        return PolicyHumanSubject.model_validate(subject.model_dump(mode="json"))
    return PolicyServiceSubject.model_validate(subject.model_dump(mode="json"))


def _role_allowed(registration: ToolRegistration, context: VisibilityContext) -> bool:
    return isinstance(context.subject, HumanSubject) and (
        context.subject.role in registration.definition.allowed_roles
    )


def _local_dimensions(
    registration: ToolRegistration,
    context: VisibilityContext,
) -> tuple[bool, bool, bool, bool]:
    definition = registration.definition
    phase_allowed = context.phase in definition.allowed_phases
    environment_supported = set(definition.required_hardware_capabilities).issubset(
        context.hardware_capabilities
    )
    provider_enabled = set(definition.required_providers).issubset(context.enabled_providers)
    feature_flags_enabled = set(definition.required_feature_flags).issubset(
        context.enabled_feature_flags
    )
    return phase_allowed, environment_supported, provider_enabled, feature_flags_enabled


class ToolRegistry:
    """Resolve only explicitly registered, typed Tool contracts."""

    def __init__(self, registrations: Iterable[ToolRegistration]) -> None:
        indexed: dict[str, ToolRegistration] = {}
        for registration in registrations:
            name = str(registration.definition.name)
            if name in indexed:
                raise ValueError(DUPLICATE_TOOL)
            indexed[name] = registration
        self._registrations = indexed

    def get(self, name: ToolName) -> ToolRegistration | None:
        return self._registrations.get(str(name))

    def resolve_visible_tools(
        self,
        *,
        context: VisibilityContext,
        request_id: str,
        current_time: datetime,
        policy: PolicyClient,
    ) -> ToolSetSnapshot:
        """Evaluate every fixed Tool and return a reproducible visibility snapshot."""
        entries: list[ToolSetEntry] = []
        decision_ids: list[str] = []
        for name in sorted(self._registrations):
            registration = self._registrations[name]
            definition = registration.definition
            (
                phase_allowed,
                environment_supported,
                provider_enabled,
                feature_flags_enabled,
            ) = _local_dimensions(registration, context)
            policy_decision = policy.evaluate(
                PolicyInput(
                    request_id=request_id,
                    purpose=PolicyEvaluationPurpose.VISIBILITY,
                    current_time=current_time,
                    phase=context.phase,
                    subject=_policy_subject(context.subject),
                    tool=PolicyTool(
                        name=definition.name,
                        schema_version=definition.input_schema_version,
                        risk_level=definition.risk_level,
                        allowed_phases=tuple(sorted(definition.allowed_phases, key=str)),
                        allowed_roles=tuple(sorted(definition.allowed_roles, key=str)),
                        environment_supported=environment_supported,
                        provider_enabled=provider_enabled,
                        feature_flags_enabled=feature_flags_enabled,
                    ),
                )
            )
            decision_ids.append(policy_decision.decision_id)
            locally_visible = (
                phase_allowed
                and _role_allowed(registration, context)
                and environment_supported
                and provider_enabled
                and feature_flags_enabled
                and definition.risk_level is not RiskLevel.L3
            )
            if locally_visible and policy_decision.allow:
                entries.append(
                    ToolSetEntry(
                        name=definition.name,
                        schema_version=definition.input_schema_version,
                        risk_level=definition.risk_level,
                    )
                )
        return create_toolset_snapshot(
            context=context,
            tools=tuple(entries),
            policy_decision_ids=tuple(decision_ids),
            created_at=current_time,
        )
