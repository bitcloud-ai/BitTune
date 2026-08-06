"""Fail-closed defaults for production wiring without a verified provider profile."""

from __future__ import annotations

from typing import NoReturn

from autopilot.domain.errors import ErrorEnvelope
from autopilot.domain.requirements import RequirementSpec
from autopilot.graph.model_provider import (
    BenchmarkIntent,
    FailureAnalysis,
    ModelProviderError,
    ReportDraft,
)
from autopilot.graph.reconciliation import ReconciliationResult
from autopilot.graph.state import AutopilotState


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


__all__ = ["UnavailableModelProvider", "UnavailableReconciler"]
