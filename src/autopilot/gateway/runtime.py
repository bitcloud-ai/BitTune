"""PostgreSQL-backed Tool Gateway assembly for the production API."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from autopilot.capabilities.benchmark.domain.models import BenchmarkExecutionSpecification
from autopilot.capabilities.deployment.domain.models import DeploymentExecutionSpecification
from autopilot.capabilities.environment.domain.models import (
    EnvironmentExecutionSpecification,
    EnvironmentInspectionSpecification,
)
from autopilot.capabilities.optimization.domain.models import OptimizationExecutionSpecification
from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.enums import (
    ExperimentPhase,
    ExperimentStatus,
    JobKind,
    JobStatus,
    PlanKind,
    PlanStatus,
    RiskLevel,
)
from autopilot.domain.hashing import compute_content_hash
from autopilot.domain.identifiers import (
    ApprovalId,
    ExperimentId,
    JobId,
    PlanHash,
    PlanId,
    ToolName,
    ToolSetId,
)
from autopilot.domain.jobs import JobRecord
from autopilot.domain.plans import (
    ExecutionSpecification,
    PlanEnvelope,
    compute_plan_envelope_hash,
)
from autopilot.domain.requirements import RequirementSpec
from autopilot.gateway.approval_ports import ApprovalRepository
from autopilot.gateway.errors import ToolDispatchError, WorkflowStateError
from autopilot.gateway.models import (
    AuthorizedJobControl,
    AuthorizedReadOnlyCall,
    JobAuthorizationDraft,
    JobIdempotencyClaim,
    PlanAuthorizationMaterial,
    ToolSetSnapshot,
)
from autopilot.gateway.mvp_tools import (
    DomainJobWriter,
    DomainPlanResult,
    DomainPlanWriter,
    ExperimentPlanResult,
    ExperimentPlanWriter,
    JobCancelRequest,
    JobSubmissionResult,
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
from autopilot.infrastructure.database.models import ExperimentRow, PlanRow
from autopilot.infrastructure.database.repositories import (
    SqlAlchemyApprovalRepository,
    SqlAlchemyAuditRepository,
    SqlAlchemyJobRepository,
)
from autopilot.jobs.models import AuditEvent
from autopilot.policy.models import PolicyApproval
from autopilot.policy.ports import PolicyClient

EXPERIMENT_NOT_FOUND = "Experiment does not exist"
EXPERIMENT_PHASE_INVALID = "Experiment phase is invalid"
PLAN_SPECIFICATION_INVALID = "Plan specification does not match its domain kind"
EXPERIMENT_REQUIREMENTS_MISSING = "Experiment requirements are not available"
JOB_ACTION_NOT_SUPPORTED = "Job action is not supported by the MVP dispatcher"
JOB_REPLAY_MISMATCH = "persisted Job does not match the authorized request"


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


class _SessionExperimentPlans(ExperimentPlanWriter):
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def create(
        self,
        requirements: RequirementSpec,
        authorization: AuthorizedReadOnlyCall,
    ) -> ExperimentPlanResult:
        with self._sessions.begin() as session:
            row = session.get(
                ExperimentRow,
                str(authorization.experiment_id),
                with_for_update=True,
            )
            if row is None:
                raise ToolDispatchError(EXPERIMENT_NOT_FOUND)
            if row.phase != ExperimentPhase.REQUIREMENTS.value:
                raise ToolDispatchError(EXPERIMENT_PHASE_INVALID)
            serialized_requirements = requirements.model_dump(mode="json")
            state = dict(row.graph_state_json)
            state["requirements"] = serialized_requirements
            state["phase"] = ExperimentPhase.ENVIRONMENT.value
            state["status"] = ExperimentStatus.ACTIVE.value
            row.requirements_json = serialized_requirements
            row.graph_state_json = state
            row.phase = ExperimentPhase.ENVIRONMENT.value
            row.status = ExperimentStatus.ACTIVE.value
            row.updated_at = authorization.authorized_at
            session.flush()
        return ExperimentPlanResult(
            experiment_id=authorization.experiment_id,
            requirements_hash=compute_content_hash(requirements),
        )


class _SessionDomainPlans(DomainPlanWriter):
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @staticmethod
    def _matches_kind(kind: PlanKind, specification: ExecutionSpecification) -> bool:
        expected_types: dict[PlanKind, type[ExecutionSpecification]] = {
            PlanKind.ENVIRONMENT: EnvironmentExecutionSpecification,
            PlanKind.DEPLOYMENT: DeploymentExecutionSpecification,
            PlanKind.BENCHMARK: BenchmarkExecutionSpecification,
            PlanKind.OPTIMIZATION: OptimizationExecutionSpecification,
        }
        expected = expected_types.get(kind)
        return expected is not None and isinstance(specification, expected)

    @staticmethod
    def _execution_specification(
        row: ExperimentRow,
        specification: BaseModel,
    ) -> ExecutionSpecification:
        if isinstance(specification, EnvironmentInspectionSpecification):
            if row.requirements_json is None:
                raise ToolDispatchError(EXPERIMENT_REQUIREMENTS_MISSING)
            requirements = RequirementSpec.model_validate(row.requirements_json)
            return EnvironmentExecutionSpecification(
                provider_version=specification.provider_version,
                adapter_version=specification.adapter_version,
                provider_profile_version=specification.provider_profile_version,
                budget=requirements.budget,
                inspection=specification,
            )
        if isinstance(specification, ExecutionSpecification):
            return specification
        raise ToolDispatchError(PLAN_SPECIFICATION_INVALID)

    def create(
        self,
        *,
        kind: PlanKind,
        risk_level: RiskLevel,
        specification: BaseModel,
        authorization: AuthorizedReadOnlyCall,
    ) -> DomainPlanResult:
        with self._sessions.begin() as session:
            row = session.get(
                ExperimentRow,
                str(authorization.experiment_id),
                with_for_update=True,
            )
            if row is None:
                raise ToolDispatchError(EXPERIMENT_NOT_FOUND)
            execution = self._execution_specification(row, specification)
            if not self._matches_kind(kind, execution):
                raise ToolDispatchError(PLAN_SPECIFICATION_INVALID)
            plan_id = PlanId.new()
            status = PlanStatus.DRAFT if risk_level is RiskLevel.L2 else PlanStatus.APPROVED
            plan_hash = compute_plan_envelope_hash(
                plan_id=plan_id,
                experiment_id=authorization.experiment_id,
                kind=kind,
                risk_level=risk_level,
                execution_specification=execution,
            )
            envelope = PlanEnvelope[ExecutionSpecification](
                plan_id=plan_id,
                experiment_id=authorization.experiment_id,
                kind=kind,
                status=status,
                risk_level=risk_level,
                execution_specification=execution,
                plan_hash=plan_hash,
                created_at=authorization.authorized_at,
            )
            session.add(
                PlanRow(
                    id=str(plan_id),
                    experiment_id=str(authorization.experiment_id),
                    kind=kind.value,
                    schema_version=envelope.schema_version,
                    body_json=envelope.model_dump(mode="json"),
                    plan_hash=str(plan_hash),
                    risk_level=risk_level.value,
                    status=status.value,
                    approved_by=None,
                    created_at=authorization.authorized_at,
                )
            )
            session.flush()
        return DomainPlanResult(
            experiment_id=authorization.experiment_id,
            plan_id=plan_id,
            kind=kind,
            status=status,
            risk_level=risk_level,
            plan_hash=plan_hash,
            execution_schema_version=execution.schema_version,
            requires_approval=risk_level is RiskLevel.L2,
        )


class _SessionJobs(DomainJobWriter):
    _KINDS: ClassVar[dict[str, JobKind]] = {
        "start_environment_inspection": JobKind.ENVIRONMENT,
        "cancel_environment_inspection": JobKind.ENVIRONMENT,
        "start_deployment": JobKind.DEPLOYMENT,
        "cancel_deployment": JobKind.DEPLOYMENT,
        "start_benchmark": JobKind.BENCHMARK,
        "cancel_benchmark": JobKind.BENCHMARK,
        "start_optimization": JobKind.OPTIMIZATION,
        "cancel_optimization": JobKind.OPTIMIZATION,
    }

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def _kind(cls, registration: ToolRegistration) -> JobKind:
        kind = cls._KINDS.get(str(registration.definition.name))
        if kind is None:
            raise ToolDispatchError(JOB_ACTION_NOT_SUPPORTED)
        return kind

    @staticmethod
    def _result(job: JobRecord, *, created: bool) -> JobSubmissionResult:
        return JobSubmissionResult(
            experiment_id=job.experiment_id,
            job_id=job.job_id,
            plan_id=job.plan_id,
            status=job.status,
            created=created,
        )

    def enqueue(
        self,
        registration: ToolRegistration,
        authorization: JobAuthorizationDraft,
    ) -> JobSubmissionResult:
        job = JobRecord(
            job_id=JobId.new(),
            experiment_id=authorization.experiment_id,
            plan_id=authorization.plan_id,
            kind=self._kind(registration),
            status=JobStatus.QUEUED,
            submitted_at=authorization.authorized_at,
        )
        with self._sessions.begin() as session:
            result = SqlAlchemyJobRepository(session).enqueue(job, authorization=authorization)
            return self._result(result.job, created=result.created)

    def replay(
        self,
        registration: ToolRegistration,
        job_id: JobId,
        authorization: JobAuthorizationDraft,
    ) -> JobSubmissionResult:
        expected_kind = self._kind(registration)
        with self._sessions() as session:
            job = SqlAlchemyJobRepository(session).get(job_id)
        if (
            job is None
            or job.experiment_id != authorization.experiment_id
            or job.plan_id != authorization.plan_id
            or job.kind is not expected_kind
        ):
            raise ToolDispatchError(JOB_REPLAY_MISMATCH)
        return self._result(job, created=False)

    def cancel(
        self,
        registration: ToolRegistration,
        request: JobCancelRequest,
        authorization: AuthorizedJobControl,
    ) -> JobSubmissionResult:
        expected_kind = self._kind(registration)
        with self._sessions.begin() as session:
            repository = SqlAlchemyJobRepository(session)
            job = repository.get(request.job_id)
            if job is None or job.experiment_id != authorization.experiment_id:
                raise ToolDispatchError(JOB_REPLAY_MISMATCH)
            if job.kind is not expected_kind:
                raise ToolDispatchError(JOB_REPLAY_MISMATCH)
            cancelled = repository.request_cancel(job_id=request.job_id)
            return self._result(cancelled, created=False)


class _DeferredWorkerResources(ResourceReservationPort):
    """Persisted queue admission; the leased Worker acquires the physical GPU lock."""

    def reserve(self, authorization: JobAuthorizationDraft) -> None:
        del authorization


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
    dispatcher: ToolDispatcher = MvpToolDispatcher(
        provider_statuses=provider_statuses,
        experiment_plans=_SessionExperimentPlans(sessions),
        plans=_SessionDomainPlans(sessions),
        jobs=_SessionJobs(sessions),
    )
    dependencies = GatewayDependencies(
        registry=ToolRegistry(registrations),
        policy=policy,
        workflow=_SessionWorkflow(sessions),
        toolsets=_SessionToolSets(sessions),
        plans=_SessionPlans(sessions),
        approvals=_SessionApprovals(sessions),
        idempotency=_SessionIdempotency(sessions),
        resources=_DeferredWorkerResources(),
        dispatcher=dispatcher,
        audit=_SessionAudit(sessions),
    )
    return ProductionGatewayAssembly(
        gateway=ToolGateway(dependencies, budget_ceiling=budget_ceiling, clock=clock),
        registrations=registrations,
    )


__all__ = ["ProductionGatewayAssembly", "build_production_gateway"]
