"""Versioned deployment plans, profiles, and compiled DTOs."""

from typing import Final, Literal, Self

from pydantic import Field, model_validator

from autopilot.capabilities.deployment.domain.enums import DeploymentHealthCheck
from autopilot.domain.base import NonEmptyStr, StrictModel
from autopilot.domain.candidates import (
    DeploymentCandidate,
    VllmTuningSpec,
    vllm_scheduler_parameters_are_compatible,
)
from autopilot.domain.enums import RiskLevel
from autopilot.domain.identifiers import CandidateId, ImageDigest, Sha256Digest
from autopilot.domain.models import ModelRef
from autopilot.domain.plans import ExecutionSpecification
from autopilot.domain.workloads import WorkloadSpec

INVALID_PROFILE_TIMEOUTS = "deployment profile timeouts must be internally consistent"
INVALID_PROFILE_VALUES = "deployment profile supported values must be unique and ordered"
INVALID_RUNTIME_LIMITS = "deployment runtime timeouts must fit within the task timeout"
INVALID_HEALTH_POLICY = "deployment health policy must contain every mandatory check exactly once"
INVALID_COMPILED_SCHEDULER_BATCH = "compiled vLLM arguments violate the scheduler batch rule"

REQUIRED_DEPLOYMENT_HEALTH_CHECKS: Final = (
    DeploymentHealthCheck.PROCESS,
    DeploymentHealthCheck.HTTP,
    DeploymentHealthCheck.MODEL_LIST,
    DeploymentHealthCheck.MINIMAL_COMPLETION,
    DeploymentHealthCheck.NON_EMPTY_COMPLETION,
    DeploymentHealthCheck.GPU_MEMORY,
    DeploymentHealthCheck.FATAL_LOG,
)


class VllmVersionProfile(StrictModel):
    schema_version: Literal["vllm-version-profile/v1"] = "vllm-version-profile/v1"
    profile_version: NonEmptyStr
    provider_version: NonEmptyStr
    adapter_version: NonEmptyStr
    engine_image: ImageDigest
    rtx_5090_verified: Literal[True]
    max_model_len_upper_bound: int = Field(ge=1, le=50_000_000)
    gpu_memory_utilization_min: float = Field(ge=0.80, le=0.94)
    gpu_memory_utilization_max: float = Field(ge=0.80, le=0.94)
    supported_max_num_seqs: tuple[Literal[4, 8, 16, 32], ...] = Field(min_length=1, max_length=4)
    supported_max_num_batched_tokens: tuple[Literal[2048, 4096, 8192, 16384], ...] = Field(
        min_length=1, max_length=4
    )
    supports_chunked_prefill: bool
    container_port: Literal[8000]
    pid_limit: int = Field(ge=64, le=65_536)
    startup_timeout_seconds: int = Field(ge=1, le=1_800)
    max_task_timeout_seconds: int = Field(ge=1, le=1_800)
    health_check_timeout_seconds: int = Field(ge=1, le=300)

    @model_validator(mode="after")
    def validate_profile(self) -> Self:
        if (
            self.gpu_memory_utilization_min > self.gpu_memory_utilization_max
            or self.startup_timeout_seconds > self.max_task_timeout_seconds
            or self.health_check_timeout_seconds > self.startup_timeout_seconds
        ):
            raise ValueError(INVALID_PROFILE_TIMEOUTS)
        if (
            tuple(sorted(set(self.supported_max_num_seqs))) != self.supported_max_num_seqs
            or tuple(sorted(set(self.supported_max_num_batched_tokens)))
            != self.supported_max_num_batched_tokens
        ):
            raise ValueError(INVALID_PROFILE_VALUES)
        return self


class DeploymentExecutionSpecification(ExecutionSpecification):
    schema_version: Literal["deployment-execution-specification/v1"] = (
        "deployment-execution-specification/v1"
    )
    provider: Literal["vllm"] = "vllm"
    operation: Literal["start"] = "start"
    candidate: DeploymentCandidate
    workload: WorkloadSpec


class CompiledVllmArguments(StrictModel):
    tensor_parallel_size: Literal[1] = 1
    max_model_len: int = Field(ge=1, le=50_000_000)
    gpu_memory_utilization: float = Field(ge=0.80, le=0.94)
    max_num_seqs: Literal[4, 8, 16, 32]
    max_num_batched_tokens: Literal[2048, 4096, 8192, 16384]
    enable_chunked_prefill: bool
    trust_remote_code: Literal[False] = False

    @model_validator(mode="after")
    def validate_scheduler_batch(self) -> Self:
        if not vllm_scheduler_parameters_are_compatible(
            max_model_len=self.max_model_len,
            max_num_batched_tokens=self.max_num_batched_tokens,
            enable_chunked_prefill=self.enable_chunked_prefill,
        ):
            raise ValueError(INVALID_COMPILED_SCHEDULER_BATCH)
        return self


class DeploymentRuntimeLimits(StrictModel):
    pid_limit: int = Field(ge=64, le=65_536)
    startup_timeout_seconds: int = Field(ge=1, le=1_800)
    task_timeout_seconds: int = Field(ge=1, le=1_800)
    health_check_timeout_seconds: int = Field(ge=1, le=300)
    max_disk_growth_bytes: int = Field(ge=1, le=20_000_000_000)

    @model_validator(mode="after")
    def validate_timeouts(self) -> Self:
        if (
            self.startup_timeout_seconds > self.task_timeout_seconds
            or self.health_check_timeout_seconds > self.startup_timeout_seconds
        ):
            raise ValueError(INVALID_RUNTIME_LIMITS)
        return self


class DeploymentHealthPolicy(StrictModel):
    required_checks: tuple[DeploymentHealthCheck, ...] = REQUIRED_DEPLOYMENT_HEALTH_CHECKS

    @model_validator(mode="after")
    def validate_required_checks(self) -> Self:
        if self.required_checks != REQUIRED_DEPLOYMENT_HEALTH_CHECKS:
            raise ValueError(INVALID_HEALTH_POLICY)
        return self


class CompiledVllmDeployment(StrictModel):
    schema_version: Literal["compiled-vllm-deployment/v1"] = "compiled-vllm-deployment/v1"
    provider: Literal["vllm"] = "vllm"
    provider_version: NonEmptyStr
    adapter_version: NonEmptyStr
    provider_profile_version: NonEmptyStr
    candidate_id: CandidateId
    engine_image: ImageDigest
    model_ref: ModelRef
    workload_hash: Sha256Digest
    accelerator_index: Literal[0] = 0
    exclusive_gpu: Literal[True] = True
    container_port: Literal[8000] = 8000
    arguments: CompiledVllmArguments
    runtime_limits: DeploymentRuntimeLimits
    health_policy: DeploymentHealthPolicy


class DeploymentPreview(StrictModel):
    schema_version: Literal["deployment-preview/v1"] = "deployment-preview/v1"
    compiled: CompiledVllmDeployment
    risk_level: Literal[RiskLevel.L2] = RiskLevel.L2
    requires_human_approval: Literal[True] = True


def compiled_arguments(parameters: VllmTuningSpec) -> CompiledVllmArguments:
    """Copy the five domain tuning fields into the provider DTO."""
    return CompiledVllmArguments.model_validate(parameters.model_dump())
