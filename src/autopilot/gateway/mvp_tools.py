"""Fixed MVP Tool Registry and provider-independent discovery dispatch."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from autopilot.capabilities.benchmark.tools import CreateBenchmarkPlanInput
from autopilot.capabilities.capacity.tools import CreateCapacityPlanInput
from autopilot.capabilities.deployment.tools import CreateDeploymentPlanInput
from autopilot.capabilities.environment.tools import CreateEnvironmentPlanInput
from autopilot.capabilities.evidence.tools import EvidenceQueryInput
from autopilot.capabilities.optimization.tools import CreateOptimizationPlanInput
from autopilot.domain.base import NonEmptyStr, SchemaVersion, StrictModel
from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.constraints import SloSpec
from autopilot.domain.enums import (
    ExperimentPhase,
    JobStatus,
    PlanKind,
    PlanStatus,
    RiskLevel,
    UserRole,
)
from autopilot.domain.identifiers import (
    ExperimentId,
    JobId,
    PlanHash,
    PlanId,
    Sha256Digest,
    ToolName,
)
from autopilot.domain.identities import HumanSubject
from autopilot.domain.jobs import JobRecord
from autopilot.domain.models import ModelRef
from autopilot.domain.plans import PlanExecutionRequest
from autopilot.domain.requirements import RequirementSpec
from autopilot.domain.workloads import WorkloadSpec
from autopilot.gateway.errors import ToolDispatchError
from autopilot.gateway.models import (
    AuthorizedJobControl,
    AuthorizedReadOnlyCall,
    JobAuthorizationDraft,
    ToolDefinition,
    ToolExecutionMode,
)
from autopilot.gateway.registry import ToolRegistration

ProviderStatus = Literal["verified", "not_configured", "blocked"]
PROVIDER_READ_NOT_CONFIGURED = "registered Provider read operation is not configured"
PROVIDER_EXECUTION_NOT_CONFIGURED = "Provider execution and Worker are not configured"
CANCEL_ARGUMENTS_INVALID = "cancel Tool arguments are invalid"
EXPERIMENT_PLAN_STORE_NOT_CONFIGURED = "Experiment Plan store is not configured"
EXPERIMENT_PLAN_REQUIRES_HUMAN = "Experiment Plan requires an authenticated human"


class CapabilitiesQuery(StrictModel):
    schema_version: Literal["capabilities-query/v1"] = "capabilities-query/v1"


class CreateExperimentPlanInput(StrictModel):
    schema_version: Literal["create-experiment-plan-input/v1"] = "create-experiment-plan-input/v1"
    model_ref: ModelRef
    priority: Literal["balanced", "latency", "throughput"]
    workload: WorkloadSpec
    slo: SloSpec
    budget: ExecutionBudget
    allow_model_download: bool
    allow_container_start: bool

    def requirement(self, subject: HumanSubject) -> RequirementSpec:
        return RequirementSpec(
            created_by=subject.user_id,
            model_ref=self.model_ref,
            priority=self.priority,
            workload=self.workload,
            slo=self.slo,
            budget=self.budget,
            allow_model_download=self.allow_model_download,
            allow_container_start=self.allow_container_start,
        )


class ExperimentPlanResult(StrictModel):
    schema_version: Literal["experiment-plan-result/v1"] = "experiment-plan-result/v1"
    experiment_id: ExperimentId
    requirements_hash: Sha256Digest
    next_phase: Literal[ExperimentPhase.ENVIRONMENT] = ExperimentPhase.ENVIRONMENT


class ExperimentPlanWriter(Protocol):
    def create(
        self,
        requirements: RequirementSpec,
        authorization: AuthorizedReadOnlyCall,
    ) -> ExperimentPlanResult: ...


class DomainPlanResult(StrictModel):
    schema_version: Literal["domain-plan-result/v1"] = "domain-plan-result/v1"
    experiment_id: ExperimentId
    plan_id: PlanId
    kind: PlanKind
    status: PlanStatus
    risk_level: RiskLevel
    plan_hash: PlanHash
    execution_schema_version: SchemaVersion
    requires_approval: bool


class DomainPlanWriter(Protocol):
    def create(
        self,
        *,
        kind: PlanKind,
        risk_level: RiskLevel,
        specification: BaseModel,
        authorization: AuthorizedReadOnlyCall,
    ) -> DomainPlanResult: ...


class JobSubmissionResult(StrictModel):
    schema_version: Literal["job-submission-result/v1"] = "job-submission-result/v1"
    experiment_id: ExperimentId
    job_id: JobId
    plan_id: PlanId
    status: JobStatus
    created: bool


class JobQueryResult(StrictModel):
    """Persisted Job projection returned by status and result Tools."""

    schema_version: Literal["job-query-result/v1"] = "job-query-result/v1"
    job: JobRecord


class DomainJobWriter(Protocol):
    def get(
        self,
        registration: ToolRegistration,
        query: JobQuery,
        authorization: AuthorizedReadOnlyCall,
    ) -> JobQueryResult: ...

    def enqueue(
        self,
        registration: ToolRegistration,
        authorization: JobAuthorizationDraft,
    ) -> JobSubmissionResult: ...

    def replay(
        self,
        registration: ToolRegistration,
        job_id: JobId,
        authorization: JobAuthorizationDraft,
    ) -> JobSubmissionResult: ...

    def cancel(
        self,
        registration: ToolRegistration,
        request: JobCancelRequest,
        authorization: AuthorizedJobControl,
    ) -> JobSubmissionResult: ...


class JobQuery(StrictModel):
    schema_version: Literal["job-query/v1"] = "job-query/v1"
    job_id: JobId


class JobCancelRequest(StrictModel):
    schema_version: Literal["job-cancel-request/v1"] = "job-cancel-request/v1"
    job_id: JobId


class ProviderAvailability(StrictModel):
    schema_version: Literal["provider-availability/v1"] = "provider-availability/v1"
    provider: NonEmptyStr
    status: ProviderStatus
    reason: NonEmptyStr


class MvpCapabilitiesResult(StrictModel):
    """Bounded capability discovery result that contains no host facts or secrets."""

    schema_version: Literal["mvp-capabilities-result/v1"] = "mvp-capabilities-result/v1"
    product: Literal["bittune-inference-autopilot-mvp"] = "bittune-inference-autopilot-mvp"
    hardware_scope: Literal["one_linux_host_one_rtx_5090_32gb"] = "one_linux_host_one_rtx_5090_32gb"
    supported_phases: tuple[ExperimentPhase, ...] = Field(min_length=1)
    providers: tuple[ProviderAvailability, ...] = Field(min_length=1, max_length=16)
    forbidden_actions: tuple[NonEmptyStr, ...] = Field(min_length=1, max_length=32)


@dataclass(frozen=True, slots=True)
class _ToolSpec:
    name: str
    schema_version: str
    risk_level: RiskLevel
    phases: tuple[ExperimentPhase, ...]
    roles: tuple[UserRole, ...]
    input_model: type[BaseModel]
    provider: str | None = None
    hardware: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MvpToolDispatcher:
    """Execute only the provider-independent discovery action.

    The existing capability services and Worker will replace the classified rejection for each
    Provider action after its G0 profile is fixed.  This dispatcher never invokes a Fake adapter.
    """

    provider_statuses: Mapping[str, ProviderStatus]
    experiment_plans: ExperimentPlanWriter | None = None
    plans: DomainPlanWriter | None = None
    jobs: DomainJobWriter | None = None

    def invoke_read_only(  # noqa: PLR0912
        self,
        registration: ToolRegistration,
        arguments: BaseModel,
        authorization: AuthorizedReadOnlyCall,
    ) -> BaseModel:
        tool_name = str(registration.definition.name)
        if tool_name == "create_experiment_plan":
            if self.experiment_plans is None:
                raise ToolDispatchError(EXPERIMENT_PLAN_STORE_NOT_CONFIGURED)
            if not isinstance(arguments, CreateExperimentPlanInput) or not isinstance(
                authorization.subject, HumanSubject
            ):
                raise ToolDispatchError(EXPERIMENT_PLAN_REQUIRES_HUMAN)
            return self.experiment_plans.create(
                arguments.requirement(authorization.subject),
                authorization,
            )
        plan_request: tuple[PlanKind, RiskLevel, BaseModel] | None = None
        if tool_name == "create_environment_plan" and isinstance(
            arguments, CreateEnvironmentPlanInput
        ):
            plan_request = (PlanKind.ENVIRONMENT, RiskLevel.L1, arguments.specification)
        elif tool_name == "create_capacity_plan" and isinstance(arguments, CreateCapacityPlanInput):
            plan_request = (PlanKind.CAPACITY, RiskLevel.L1, arguments.specification)
        elif tool_name == "create_deployment_plan" and isinstance(
            arguments, CreateDeploymentPlanInput
        ):
            plan_request = (PlanKind.DEPLOYMENT, RiskLevel.L2, arguments.specification)
        elif tool_name == "create_benchmark_plan" and isinstance(
            arguments, CreateBenchmarkPlanInput
        ):
            plan_request = (PlanKind.BENCHMARK, RiskLevel.L2, arguments.specification)
        elif tool_name == "create_optimization_plan" and isinstance(
            arguments, CreateOptimizationPlanInput
        ):
            plan_request = (PlanKind.OPTIMIZATION, RiskLevel.L2, arguments.specification)
        if plan_request is not None:
            if self.plans is None:
                raise ToolDispatchError(PROVIDER_READ_NOT_CONFIGURED)
            kind, risk_level, specification = plan_request
            return self.plans.create(
                kind=kind,
                risk_level=risk_level,
                specification=specification,
                authorization=authorization,
            )
        if isinstance(arguments, JobQuery):
            if self.jobs is None or not tool_name.startswith("get_"):
                raise ToolDispatchError(PROVIDER_READ_NOT_CONFIGURED)
            return self.jobs.get(registration, arguments, authorization)
        if tool_name != "get_mvp_capabilities_result":
            raise ToolDispatchError(PROVIDER_READ_NOT_CONFIGURED)
        return MvpCapabilitiesResult(
            supported_phases=tuple(ExperimentPhase),
            providers=tuple(
                ProviderAvailability(
                    provider=provider,
                    status=status,
                    reason=(
                        "G0 Provider Profile and adapter are verified"
                        if status == "verified"
                        else "fixed Provider Profile is not configured for this deployment"
                    ),
                )
                for provider, status in self.provider_statuses.items()
            ),
            forbidden_actions=(
                "execute_shell",
                "run_python",
                "docker_run",
                "docker_exec",
                "delete_path",
                "install_driver",
                "modify_kernel",
            ),
        )

    def enqueue_job(
        self,
        registration: ToolRegistration,
        arguments: BaseModel,
        authorization: JobAuthorizationDraft,
    ) -> BaseModel:
        del arguments
        if self.jobs is None or not str(registration.definition.name).startswith("start_"):
            raise ToolDispatchError(PROVIDER_EXECUTION_NOT_CONFIGURED)
        return self.jobs.enqueue(registration, authorization)

    def replay_job(
        self,
        registration: ToolRegistration,
        job_id: JobId,
        authorization: JobAuthorizationDraft,
    ) -> BaseModel:
        if self.jobs is None or not str(registration.definition.name).startswith("start_"):
            raise ToolDispatchError(PROVIDER_EXECUTION_NOT_CONFIGURED)
        return self.jobs.replay(registration, job_id, authorization)

    def cancel_job(
        self,
        registration: ToolRegistration,
        arguments: BaseModel,
        authorization: AuthorizedJobControl,
    ) -> BaseModel:
        if self.jobs is None or not str(registration.definition.name).startswith("cancel_"):
            raise ToolDispatchError(PROVIDER_EXECUTION_NOT_CONFIGURED)
        if not isinstance(arguments, JobCancelRequest):
            raise ToolDispatchError(CANCEL_ARGUMENTS_INVALID)
        return self.jobs.cancel(registration, arguments, authorization)


def _registration(spec: _ToolSpec) -> ToolRegistration:
    async_job = spec.name.startswith(("start_", "cancel_"))
    return ToolRegistration(
        definition=ToolDefinition(
            name=ToolName(root=spec.name),
            input_schema_version=SchemaVersion(spec.schema_version),
            risk_level=spec.risk_level,
            execution_mode=(
                ToolExecutionMode.ASYNC_JOB if async_job else ToolExecutionMode.READ_ONLY
            ),
            allowed_phases=spec.phases,
            allowed_roles=spec.roles,
            required_hardware_capabilities=spec.hardware,
            required_providers=() if spec.provider is None else (spec.provider,),
            requires_plan=spec.name.startswith("start_"),
        ),
        input_model=spec.input_model,
    )


def _job_tools(
    *,
    capability: str,
    phases: tuple[ExperimentPhase, ...],
    provider: str,
    start_risk: RiskLevel,
    hardware: tuple[str, ...] = (),
) -> tuple[_ToolSpec, ...]:
    read_roles = (UserRole.VIEWER, UserRole.OPERATOR, UserRole.ADMIN)
    operator_roles = (UserRole.OPERATOR, UserRole.ADMIN)
    start_name = (
        "start_environment_inspection" if capability == "environment" else f"start_{capability}"
    )
    cancel_name = (
        "cancel_environment_inspection" if capability == "environment" else f"cancel_{capability}"
    )
    return (
        _ToolSpec(
            start_name,
            "plan-execution-request/v1",
            start_risk,
            phases,
            operator_roles,
            PlanExecutionRequest,
            provider,
            hardware,
        ),
        _ToolSpec(
            f"get_{capability}_status",
            "job-query/v1",
            RiskLevel.L0,
            phases,
            read_roles,
            JobQuery,
            provider,
            hardware,
        ),
        _ToolSpec(
            f"get_{capability}_result",
            "job-query/v1",
            RiskLevel.L0,
            phases,
            read_roles,
            JobQuery,
            provider,
            hardware,
        ),
        _ToolSpec(
            cancel_name,
            "job-cancel-request/v1",
            RiskLevel.L1,
            phases,
            operator_roles,
            JobCancelRequest,
            provider,
            hardware,
        ),
    )


def mvp_tool_registrations() -> tuple[ToolRegistration, ...]:
    """Return the fixed registry declared by the six capability manifests."""
    read_roles = (UserRole.VIEWER, UserRole.OPERATOR, UserRole.ADMIN)
    operator_roles = (UserRole.OPERATOR, UserRole.ADMIN)
    environment = (ExperimentPhase.ENVIRONMENT,)
    deployment = (ExperimentPhase.DEPLOYMENT,)
    benchmark = (
        ExperimentPhase.BENCHMARK,
        ExperimentPhase.OPTIMIZATION,
        ExperimentPhase.VERIFICATION,
    )
    optimization = (ExperimentPhase.OPTIMIZATION, ExperimentPhase.VERIFICATION)
    specifications = (
        _ToolSpec(
            "get_mvp_capabilities_result",
            "capabilities-query/v1",
            RiskLevel.L0,
            tuple(ExperimentPhase),
            read_roles,
            CapabilitiesQuery,
        ),
        _ToolSpec(
            "create_experiment_plan",
            "create-experiment-plan-input/v1",
            RiskLevel.L0,
            (ExperimentPhase.REQUIREMENTS,),
            operator_roles,
            CreateExperimentPlanInput,
        ),
        _ToolSpec(
            "create_environment_plan",
            "create-environment-plan-input/v1",
            RiskLevel.L0,
            environment,
            operator_roles,
            CreateEnvironmentPlanInput,
            "nvml",
        ),
        *_job_tools(
            capability="environment",
            phases=environment,
            provider="nvml",
            start_risk=RiskLevel.L1,
        ),
        _ToolSpec(
            "create_capacity_plan",
            "create-capacity-plan-input/v1",
            RiskLevel.L0,
            (ExperimentPhase.PLANNING,),
            operator_roles,
            CreateCapacityPlanInput,
            "llm-d-planner",
            ("single_nvidia_gpu",),
        ),
        _ToolSpec(
            "create_deployment_plan",
            "create-deployment-plan-input/v1",
            RiskLevel.L0,
            deployment,
            operator_roles,
            CreateDeploymentPlanInput,
            "vllm",
            ("single_nvidia_gpu", "docker_gpu", "vllm_single_gpu_candidate"),
        ),
        *_job_tools(
            capability="deployment",
            phases=deployment,
            provider="vllm",
            start_risk=RiskLevel.L2,
            hardware=("single_nvidia_gpu", "docker_gpu", "vllm_single_gpu_candidate"),
        ),
        _ToolSpec(
            "create_benchmark_plan",
            "create-benchmark-plan-input/v1",
            RiskLevel.L0,
            benchmark,
            operator_roles,
            CreateBenchmarkPlanInput,
            "evalscope",
            ("openai_compatible_endpoint",),
        ),
        *_job_tools(
            capability="benchmark",
            phases=benchmark,
            provider="evalscope",
            start_risk=RiskLevel.L2,
            hardware=("openai_compatible_endpoint",),
        ),
        _ToolSpec(
            "create_optimization_plan",
            "create-optimization-plan-input/v1",
            RiskLevel.L0,
            (ExperimentPhase.OPTIMIZATION,),
            operator_roles,
            CreateOptimizationPlanInput,
            "optuna",
        ),
        *_job_tools(
            capability="optimization",
            phases=optimization,
            provider="optuna",
            start_risk=RiskLevel.L2,
        ),
        _ToolSpec(
            "get_trial_comparison_result",
            "evidence-query-input/v1",
            RiskLevel.L0,
            optimization,
            read_roles,
            EvidenceQueryInput,
            "mlflow",
        ),
        _ToolSpec(
            "create_champion_plan",
            "evidence-query-input/v1",
            RiskLevel.L0,
            (ExperimentPhase.VERIFICATION,),
            operator_roles,
            EvidenceQueryInput,
            "mlflow",
        ),
        _ToolSpec(
            "get_evidence_result",
            "evidence-query-input/v1",
            RiskLevel.L0,
            (ExperimentPhase.REPORT, ExperimentPhase.COMPLETED),
            read_roles,
            EvidenceQueryInput,
            "mlflow",
        ),
    )
    return tuple(_registration(specification) for specification in specifications)


def provider_statuses(*, verified: Sequence[str] = ()) -> dict[str, ProviderStatus]:
    """Build secret-free discovery status from trusted deployment configuration."""
    verified_set = set(verified)
    return {
        provider: ("verified" if provider in verified_set else "not_configured")
        for provider in (
            "nvml",
            "llm-d-planner",
            "vllm",
            "evalscope",
            "optuna",
            "mlflow",
            "opa",
        )
    }


__all__ = [
    "CapabilitiesQuery",
    "CreateExperimentPlanInput",
    "DomainJobWriter",
    "DomainPlanResult",
    "DomainPlanWriter",
    "ExperimentPlanResult",
    "ExperimentPlanWriter",
    "JobCancelRequest",
    "JobQuery",
    "JobQueryResult",
    "JobSubmissionResult",
    "MvpCapabilitiesResult",
    "MvpToolDispatcher",
    "ProviderAvailability",
    "mvp_tool_registrations",
    "provider_statuses",
]
