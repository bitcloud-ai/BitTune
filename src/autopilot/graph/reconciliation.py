"""External-state reconciliation before a Graph resumes work."""

from __future__ import annotations

from typing import Protocol

from pydantic import Field, JsonValue

from autopilot.domain.base import NonEmptyStr, StrictModel
from autopilot.graph.state import AutopilotState


class ReconciliationResult(StrictModel):
    """Small reconciliation result; raw provider state remains outside Graph State."""

    schema_version: str = "reconciliation-result/v1"
    warnings: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=16)
    active_job_ref: NonEmptyStr | None = None
    active_deployment_ref: NonEmptyStr | None = None
    requires_failure: bool = False
    failure_code: str | None = None


class ReconciliationPort(Protocol):
    def reconcile(self, state: AutopilotState) -> ReconciliationResult: ...


class NoopReconciler:
    """Explicitly safe default for unit tests; production injects the DB/Runner reconciler."""

    def reconcile(self, _state: AutopilotState) -> ReconciliationResult:
        return ReconciliationResult()


__all__ = ["NoopReconciler", "ReconciliationPort", "ReconciliationResult"]
