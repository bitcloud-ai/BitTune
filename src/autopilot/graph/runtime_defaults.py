"""Fail-closed defaults for production wiring without a verified provider profile."""

from __future__ import annotations

from typing import NoReturn

from pydantic import BaseModel

from autopilot.domain.base import utc_now
from autopilot.domain.enums import ExperimentPhase
from autopilot.domain.errors import ErrorEnvelope
from autopilot.domain.requirements import RequirementSpec
from autopilot.gateway.models import (
    GatewayEnvironment,
    ToolCallRequest,
    ToolSetSnapshot,
    VisibilityContext,
    create_toolset_snapshot,
)
from autopilot.graph.model_provider import (
    BenchmarkIntent,
    FailureAnalysis,
    ModelProviderError,
    ReportDraft,
)
from autopilot.graph.reconciliation import ReconciliationResult
from autopilot.graph.state import AutopilotState

AGENT_GATEWAY_UNAVAILABLE = "Agent Tool Gateway is not configured"


class UnavailableModelProvider:
    """Reject LLM-dependent graph work until a remote endpoint is configured."""

    @staticmethod
    def _fail() -> NoReturn:
        raise ModelProviderError

    def parse_requirements(self, message: str) -> RequirementSpec:
        del message
        self._fail()

    def propose_test_strategy(self, requirements: RequirementSpec) -> BenchmarkIntent:
        del requirements
        self._fail()

    def analyze_failure(self, error: ErrorEnvelope) -> FailureAnalysis:
        del error
        self._fail()

    def write_report(self, evidence_refs: tuple[str, ...]) -> ReportDraft:
        del evidence_refs
        self._fail()


class UnavailableReconciler:
    """Never resume active external work without an authoritative state source."""

    def reconcile(self, state: AutopilotState) -> ReconciliationResult:
        if state.get("active_job_id") or state.get("active_deployment_id"):
            return ReconciliationResult(
                requires_failure=True,
                failure_code="EXTERNAL_STATE_SOURCE_UNAVAILABLE",
            )
        return ReconciliationResult()


class UnavailableAgentGateway:
    """Fail-closed conversational gateway until trusted Gateway dependencies are assembled."""

    def available_tools(
        self,
        *,
        environment: GatewayEnvironment,
        request_id: str,
    ) -> ToolSetSnapshot:
        del request_id
        return create_toolset_snapshot(
            context=VisibilityContext(
                experiment_id=environment.experiment_id,
                subject=environment.subject,
                phase=ExperimentPhase.REQUIREMENTS,
                hardware_capabilities=environment.hardware_capabilities,
                enabled_providers=environment.enabled_providers,
                enabled_feature_flags=environment.enabled_feature_flags,
            ),
            tools=(),
            policy_decision_ids=("no-tools-profile",),
            created_at=utc_now(),
        )

    def invoke(
        self,
        *,
        request: ToolCallRequest,
        environment: GatewayEnvironment,
    ) -> BaseModel:
        del request, environment
        raise RuntimeError(AGENT_GATEWAY_UNAVAILABLE)


__all__ = ["UnavailableAgentGateway", "UnavailableModelProvider", "UnavailableReconciler"]
