"""PostgreSQL-backed Tool Gateway assembly for the production API."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn

from sqlalchemy.orm import Session, sessionmaker

from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.enums import ExperimentPhase
from autopilot.domain.identifiers import (
    ApprovalId,
    ExperimentId,
    PlanHash,
    PlanId,
    ToolName,
    ToolSetId,
)
from autopilot.gateway.approval_ports import ApprovalRepository
from autopilot.gateway.errors import ResourceUnavailableError, WorkflowStateError
from autopilot.gateway.models import (
    JobAuthorizationDraft,
    JobIdempotencyClaim,
    PlanAuthorizationMaterial,
    ToolSetSnapshot,
)
from autopilot.gateway.mvp_tools import (
    MvpToolDispatcher,
    ProviderStatus,
    mvp_tool_registrations,
)
from autopilot.gateway.ports import (
    ApprovalAuthorizationPort,
    GatewayAuditSink,
    JobIdempotencyPort,
    PlanAuthorizationRepository,
    ResourceReservationPort,
    ToolDispatcher,
    ToolSetSnapshotRepository,
    WorkflowStateReader,
)
from autopilot.gateway.registry import ToolRegistration, ToolRegistry
from autopilot.gateway.service import GatewayDependencies, ToolGateway
from autopilot.infrastructure.database.approval_authorization import (
    DatabaseApprovalAuthorizationAdapter,
)
from autopilot.infrastructure.database.gateway_repositories import (
    SqlAlchemyJobIdempotencyGate,
    SqlAlchemyPlanAuthorizationRepository,
    SqlAlchemyToolSetSnapshotRepository,
)
from autopilot.infrastructure.database.models import ExperimentRow
from autopilot.infrastructure.database.repositories import (
    SqlAlchemyApprovalRepository,
    SqlAlchemyAuditRepository,
)
from autopilot.jobs.models import AuditEvent
from autopilot.policy.models import PolicyApproval
from autopilot.policy.ports import PolicyClient

EXPERIMENT_NOT_FOUND = "Experiment does not exist"
EXPERIMENT_PHASE_INVALID = "Experiment phase is invalid"
RESOURCE_RESERVATION_NOT_CONFIGURED = "resource reservation service is not configured"


class _SessionWorkflow(WorkflowStateReader):
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def current_phase(self, experiment_id: ExperimentId) -> ExperimentPhase:
        with self._sessions() as session:
            row = session.get(ExperimentRow, str(experiment_id))
            if row is None:
                raise WorkflowStateError(EXPERIMENT_NOT_FOUND)
            try:
                return ExperimentPhase(row.phase)
            except ValueError as error:
                raise WorkflowStateError(EXPERIMENT_PHASE_INVALID) from error


class _SessionToolSets(ToolSetSnapshotRepository):
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def add(self, snapshot: ToolSetSnapshot) -> None:
        with self._sessions.begin() as session:
            SqlAlchemyToolSetSnapshotRepository(session).add(snapshot)

    def get(self, tool_set_id: ToolSetId) -> ToolSetSnapshot | None:
        with self._sessions() as session:
            return SqlAlchemyToolSetSnapshotRepository(session).get(tool_set_id)


class _SessionPlans(PlanAuthorizationRepository):
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def get_for_execution(
        self,
        *,
        experiment_id: ExperimentId,
        plan_id: PlanId,
        expected_plan_hash: PlanHash,
    ) -> PlanAuthorizationMaterial:
        with self._sessions.begin() as session:
            return SqlAlchemyPlanAuthorizationRepository(session).get_for_execution(
                experiment_id=experiment_id,
                plan_id=plan_id,
                expected_plan_hash=expected_plan_hash,
            )


class _SessionApprovals(ApprovalAuthorizationPort):
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def _adapter(self, session: Session) -> DatabaseApprovalAuthorizationAdapter:
        repository: ApprovalRepository = SqlAlchemyApprovalRepository(session)
        return DatabaseApprovalAuthorizationAdapter(repository)

    def get_candidate(
        self,
        *,
        experiment_id: ExperimentId,
        plan_id: PlanId,
        plan_hash: PlanHash,
        action: ToolName,
    ) -> PolicyApproval | None:
        with self._sessions.begin() as session:
            return self._adapter(session).get_candidate(
                experiment_id=experiment_id,
                plan_id=plan_id,
                plan_hash=plan_hash,
                action=action,
            )

    def require_valid_for_execution(
        self,
        *,
        approval_id: ApprovalId,
        experiment_id: ExperimentId,
        plan_id: PlanId,
        plan_hash: PlanHash,
        action: ToolName,
    ) -> ApprovalId:
        with self._sessions.begin() as session:
            return self._adapter(session).require_valid_for_execution(
                approval_id=approval_id,
                experiment_id=experiment_id,
                plan_id=plan_id,
                plan_hash=plan_hash,
                action=action,
            )


class _SessionIdempotency(JobIdempotencyPort):
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def claim(self, authorization: JobAuthorizationDraft) -> JobIdempotencyClaim:
        with self._sessions.begin() as session:
            return SqlAlchemyJobIdempotencyGate(session).claim(authorization)


class _SessionAudit(GatewayAuditSink):
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def append(self, event: AuditEvent) -> None:
        with self._sessions.begin() as session:
            SqlAlchemyAuditRepository(session).append(event)


class _UnavailableResources(ResourceReservationPort):
    """Keep high-cost actions denied until the real resource coordinator is injected."""

    def reserve(self, authorization: JobAuthorizationDraft) -> NoReturn:
        del authorization
        raise ResourceUnavailableError(RESOURCE_RESERVATION_NOT_CONFIGURED)


@dataclass(frozen=True, slots=True)
class ProductionGatewayAssembly:
    gateway: ToolGateway
    registrations: tuple[ToolRegistration, ...]


def build_production_gateway(
    *,
    sessions: sessionmaker[Session],
    policy: PolicyClient,
    budget_ceiling: ExecutionBudget,
    provider_statuses: Mapping[str, ProviderStatus],
    clock: Callable[[], datetime],
) -> ProductionGatewayAssembly:
    """Assemble the existing Gateway pipeline without exposing Runner or Provider clients."""
    registrations = mvp_tool_registrations()
    dispatcher: ToolDispatcher = MvpToolDispatcher(provider_statuses=provider_statuses)
    dependencies = GatewayDependencies(
        registry=ToolRegistry(registrations),
        policy=policy,
        workflow=_SessionWorkflow(sessions),
        toolsets=_SessionToolSets(sessions),
        plans=_SessionPlans(sessions),
        approvals=_SessionApprovals(sessions),
        idempotency=_SessionIdempotency(sessions),
        resources=_UnavailableResources(),
        dispatcher=dispatcher,
        audit=_SessionAudit(sessions),
    )
    return ProductionGatewayAssembly(
        gateway=ToolGateway(dependencies, budget_ceiling=budget_ceiling, clock=clock),
        registrations=registrations,
    )


__all__ = ["ProductionGatewayAssembly", "build_production_gateway"]
