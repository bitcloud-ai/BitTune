from datetime import UTC, datetime
from typing import Literal

import pytest

from autopilot.domain.base import StrictModel
from autopilot.domain.enums import ExperimentPhase, RiskLevel, UserRole
from autopilot.domain.identifiers import ExperimentId, ToolName, UserId
from autopilot.domain.identities import HumanSubject, ServiceSubject
from autopilot.gateway.models import ToolDefinition, ToolExecutionMode, VisibilityContext
from autopilot.gateway.registry import ToolRegistration, ToolRegistry
from autopilot.policy.models import (
    PolicyDecision,
    PolicyInput,
    PolicyReasonCode,
    PolicyRequirements,
)


class QueryInput(StrictModel):
    schema_version: Literal["query/v1"] = "query/v1"


class AllowingPolicy:
    def __init__(self, denied: ToolName | None = None) -> None:
        self.denied = denied
        self.calls = 0

    def evaluate(self, policy_input: PolicyInput) -> PolicyDecision:
        self.calls += 1
        allow = policy_input.tool.name != self.denied
        return PolicyDecision(
            decision_id=f"decision-{self.calls}",
            allow=allow,
            reason_code=(PolicyReasonCode.ALLOW if allow else PolicyReasonCode.POLICY_DENIED),
            requirements=PolicyRequirements(human_approval=False),
        )


def definition(*, risk: RiskLevel = RiskLevel.L0) -> ToolDefinition:
    return ToolDefinition(
        name=ToolName(root="get_benchmark_result"),
        input_schema_version="query/v1",
        risk_level=risk,
        execution_mode=ToolExecutionMode.READ_ONLY,
        allowed_phases=(ExperimentPhase.BENCHMARK,),
        allowed_roles=(UserRole.VIEWER,),
        required_hardware_capabilities=("openai_endpoint",),
        required_providers=("evalscope",),
        required_feature_flags=("benchmark",),
        requires_plan=False,
    )


def context() -> VisibilityContext:
    return VisibilityContext(
        experiment_id=ExperimentId.new(),
        subject=HumanSubject(user_id=UserId.new(), role=UserRole.VIEWER),
        phase=ExperimentPhase.BENCHMARK,
        hardware_capabilities=frozenset({"openai_endpoint"}),
        enabled_providers=frozenset({"evalscope"}),
        enabled_feature_flags=frozenset({"benchmark"}),
    )


def resolve(
    tool: ToolDefinition,
    visibility_context: VisibilityContext,
    policy: AllowingPolicy | None = None,
):
    registry = ToolRegistry((ToolRegistration(tool, QueryInput),))
    return registry.resolve_visible_tools(
        context=visibility_context,
        request_id="visibility-request",
        current_time=datetime(2026, 8, 6, tzinfo=UTC),
        policy=policy or AllowingPolicy(),
    )


def test_visibility_requires_all_six_dimensions() -> None:
    baseline = context()
    assert len(resolve(definition(), baseline).tools) == 1

    variants = (
        baseline.model_copy(update={"phase": ExperimentPhase.DEPLOYMENT}),
        baseline.model_copy(
            update={"subject": HumanSubject(user_id=UserId.new(), role=UserRole.OPERATOR)}
        ),
        baseline.model_copy(update={"hardware_capabilities": frozenset()}),
        baseline.model_copy(update={"enabled_providers": frozenset()}),
        baseline.model_copy(update={"enabled_feature_flags": frozenset()}),
    )
    assert all(not resolve(definition(), variant).tools for variant in variants)

    denied = AllowingPolicy(denied=ToolName(root="get_benchmark_result"))
    assert not resolve(definition(), baseline, denied).tools


def test_service_subject_and_l3_are_never_visible() -> None:
    service_context = context().model_copy(
        update={"subject": ServiceSubject(service_name="autopilot-worker")}
    )

    assert not resolve(definition(), service_context).tools
    assert not resolve(definition(risk=RiskLevel.L3), context()).tools


def test_toolset_version_is_stable_and_binds_context() -> None:
    visibility_context = context()
    first = resolve(definition(), visibility_context, AllowingPolicy())
    second = resolve(definition(), visibility_context, AllowingPolicy())
    changed = resolve(
        definition(),
        visibility_context.model_copy(update={"enabled_feature_flags": frozenset()}),
        AllowingPolicy(),
    )

    assert first.tool_set_id != second.tool_set_id
    assert first.tool_set_version == second.tool_set_version
    assert first.policy_decision_ids == ("decision-1",)
    assert changed.tool_set_version != first.tool_set_version


def test_registry_rejects_duplicate_names_and_schema_mismatch() -> None:
    registration = ToolRegistration(definition(), QueryInput)
    with pytest.raises(ValueError, match="duplicate"):
        ToolRegistry((registration, registration))

    mismatched = definition().model_copy(update={"input_schema_version": "other/v1"})
    with pytest.raises(ValueError, match="Schema Version"):
        ToolRegistration(mismatched, QueryInput)


def test_unknown_tool_is_not_resolved() -> None:
    registry = ToolRegistry((ToolRegistration(definition(), QueryInput),))

    assert registry.get(ToolName(root="get_deployment_result")) is None
