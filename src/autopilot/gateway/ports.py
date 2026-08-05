"""Ports required by the mandatory Tool Gateway enforcement pipeline."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from autopilot.domain.enums import ExperimentPhase
from autopilot.domain.identifiers import (
    ApprovalId,
    ExperimentId,
    JobId,
    PlanHash,
    PlanId,
    ToolName,
    ToolSetId,
)
from autopilot.gateway.models import (
    AuthorizedReadOnlyCall,
    JobAuthorizationDraft,
    JobAuthorizationRecord,
    JobIdempotencyClaim,
    PlanAuthorizationMaterial,
    ToolSetSnapshot,
)
from autopilot.gateway.registry import ToolRegistration
from autopilot.jobs.models import AuditEvent
from autopilot.policy.models import PolicyApproval


class WorkflowStateReader(Protocol):
    def current_phase(self, experiment_id: ExperimentId) -> ExperimentPhase: ...


class ToolSetSnapshotRepository(Protocol):
    def add(self, snapshot: ToolSetSnapshot) -> None: ...

    def get(self, tool_set_id: ToolSetId) -> ToolSetSnapshot | None: ...


class PlanAuthorizationRepository(Protocol):
    def get_for_execution(
        self,
        *,
        experiment_id: ExperimentId,
        plan_id: PlanId,
        expected_plan_hash: PlanHash,
    ) -> PlanAuthorizationMaterial: ...


class JobAuthorizationRepository(Protocol):
    """Persist and reload immutable evidence that a queued Job was authorized."""

    def add(self, authorization: JobAuthorizationRecord) -> None: ...

    def get(self, job_id: JobId) -> JobAuthorizationRecord | None: ...


class JobIdempotencyPort(Protocol):
    """Serialize an idempotency key before any resource reservation occurs."""

    def claim(self, authorization: JobAuthorizationDraft) -> JobIdempotencyClaim: ...


class ApprovalAuthorizationPort(Protocol):
    def get_candidate(
        self,
        *,
        experiment_id: ExperimentId,
        plan_id: PlanId,
        plan_hash: PlanHash,
        action: ToolName,
    ) -> PolicyApproval | None: ...

    def require_valid_for_execution(
        self,
        *,
        approval_id: ApprovalId,
        experiment_id: ExperimentId,
        plan_id: PlanId,
        plan_hash: PlanHash,
        action: ToolName,
    ) -> ApprovalId: ...


class ResourceReservationPort(Protocol):
    def reserve(self, authorization: JobAuthorizationDraft) -> None: ...


class ToolDispatcher(Protocol):
    def invoke_read_only(
        self,
        registration: ToolRegistration,
        arguments: BaseModel,
        authorization: AuthorizedReadOnlyCall,
    ) -> BaseModel: ...

    def enqueue_job(
        self,
        registration: ToolRegistration,
        arguments: BaseModel,
        authorization: JobAuthorizationDraft,
    ) -> BaseModel: ...

    def replay_job(
        self,
        registration: ToolRegistration,
        job_id: JobId,
        authorization: JobAuthorizationDraft,
    ) -> BaseModel: ...


class GatewayAuditSink(Protocol):
    """Persist each record independently so a denied main transaction cannot erase it."""

    def append(self, event: AuditEvent) -> None: ...
