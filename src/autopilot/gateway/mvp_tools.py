"""Fixed MVP Tool Registry and provider-independent discovery dispatch."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from autopilot.capabilities.benchmark.tools import CreateBenchmarkPlanInput
from autopilot.capabilities.capacity.tools import CreateCapacityPlanInput
from autopilot.capabilities.deployment.tools import CreateDeploymentPlanInput
from autopilot.capabilities.environment.tools import CreateEnvironmentPlanInput
from autopilot.capabilities.evidence.tools import EvidenceQueryInput
from autopilot.capabilities.optimization.tools import CreateOptimizationPlanInput
from autopilot.domain.base import NonEmptyStr, SchemaVersion, StrictModel
from autopilot.domain.enums import ExperimentPhase, RiskLevel, UserRole
from autopilot.domain.identifiers import JobId, ToolName
from autopilot.domain.plans import PlanExecutionRequest
from autopilot.gateway.errors import ToolDispatchError
from autopilot.gateway.models import (
    AuthorizedReadOnlyCall,
    JobAuthorizationDraft,
    ToolDefinition,
    ToolExecutionMode,
)
from autopilot.gateway.registry import ToolRegistration

ProviderStatus = Literal["verified", "not_configured", "blocked"]
PROVIDER_READ_NOT_CONFIGURED = "registered Provider read operation is not configured"
PROVIDER_EXECUTION_NOT_CONFIGURED = "Provider execution and Worker are not configured"


class CapabilitiesQuery(StrictModel):
    schema_version: Literal["capabilities-query/v1"] = "capabilities-query/v1"


class JobQuery(StrictModel):
    schema_version: Literal["job-query/v1"] = "job-query/v1"
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

    def invoke_read_only(
        self,
        registration: ToolRegistration,
        arguments: BaseModel,
        authorization: AuthorizedReadOnlyCall,
    ) -> BaseModel:
        del arguments, authorization
        if str(registration.definition.name) != "get_mvp_capabilities_result":
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
        del registration, arguments, authorization
        raise ToolDispatchError(PROVIDER_EXECUTION_NOT_CONFIGURED)

    def replay_job(
        self,
        registration: ToolRegistration,
        job_id: JobId,
        authorization: JobAuthorizationDraft,
    ) -> BaseModel:
        del registration, job_id, authorization
        raise ToolDispatchError(PROVIDER_EXECUTION_NOT_CONFIGURED)


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
            requires_plan=async_job,
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
            "plan-execution-request/v1",
            start_risk,
            phases,
            operator_roles,
            PlanExecutionRequest,
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
    "JobQuery",
    "MvpCapabilitiesResult",
    "MvpToolDispatcher",
    "ProviderAvailability",
    "mvp_tool_registrations",
    "provider_statuses",
]
