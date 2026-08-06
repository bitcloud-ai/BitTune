# ruff: noqa: TRY003
"""Typed contracts for the privileged host runner.

The runner boundary deliberately does not expose process arguments, shell
fragments, host paths, arbitrary environment mappings, or Docker options.  A
request is a discriminated union of the small set of actions that the MVP
needs.  Provider-specific values arrive as already compiled, bounded fields.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, RootModel, StringConstraints, model_validator

MAX_STORAGE_COMPONENTS = 16


class RunnerModel(BaseModel):
    """Immutable, non-extensible model used at the runner boundary."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        validate_default=True,
        allow_inf_nan=False,
    )


RunnerRequestId = Annotated[
    str, StringConstraints(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
]
ContainerName = Annotated[
    str, StringConstraints(min_length=3, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$")
]
RepositoryId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"),
]


class Sha256Digest(RootModel[str]):
    """A canonical ``sha256:`` digest used by runner idempotency and plans."""

    model_config = ConfigDict(frozen=True)

    @model_validator(mode="after")
    def validate_format(self) -> Self:
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.root) is None:
            raise ValueError("digest must use sha256 followed by 64 lowercase hex characters")
        return self

    def __str__(self) -> str:
        return self.root


class ImageDigest(RootModel[str]):
    """Immutable OCI image reference.  The service still checks its allowlist."""

    model_config = ConfigDict(frozen=True)

    @model_validator(mode="after")
    def validate_format(self) -> Self:
        if re.fullmatch(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}", self.root) is None:
            raise ValueError("image must be an immutable repository-at-sha256 reference")
        return self

    def __str__(self) -> str:
        return self.root


class ModelRevision(RootModel[str]):
    """Immutable model repository revision."""

    model_config = ConfigDict(frozen=True)

    @model_validator(mode="after")
    def validate_format(self) -> Self:
        if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", self.root) is None:
            raise ValueError("model revision must be a 40- or 64-character lowercase commit hash")
        return self

    def __str__(self) -> str:
        return self.root


class RunnerAction(StrEnum):
    """The only actions that may cross the runner socket."""

    INSPECT_ENVIRONMENT = "inspect_environment"
    START_DEPLOYMENT = "start_deployment"
    STOP_DEPLOYMENT = "stop_deployment"
    GET_DEPLOYMENT_STATUS = "get_deployment_status"
    START_BENCHMARK = "start_benchmark"
    CANCEL_BENCHMARK = "cancel_benchmark"
    GET_JOB_STATUS = "get_job_status"
    GET_JOB_ARTIFACTS = "get_job_artifacts"
    START_CAPACITY_PLANNER = "start_capacity_planner"
    GET_CAPACITY_PLANNER_STATUS = "get_capacity_planner_status"
    CANCEL_CAPACITY_PLANNER = "cancel_capacity_planner"
    GET_CAPACITY_PLANNER_ARTIFACTS = "get_capacity_planner_artifacts"
    ACQUIRE_GPU_LEASE = "acquire_gpu_lease"
    HEARTBEAT_GPU_LEASE = "heartbeat_gpu_lease"
    RELEASE_GPU_LEASE = "release_gpu_lease"
    CLEANUP_TEMPORARY = "cleanup_temporary"


class StorageRoot(StrEnum):
    """Logical roots configured by the trusted runner process."""

    MODEL_CACHE = "model-cache"
    OUTPUT = "output"
    TEMPORARY = "temporary"
    RUNTIME = "runtime"


class RelativeStoragePath(RootModel[str]):
    """A path relative to a registered root, never a host path."""

    model_config = ConfigDict(frozen=True)

    @model_validator(mode="after")
    def validate_relative(self) -> Self:
        value = self.root
        if not value or "\x00" in value:
            raise ValueError("storage path must be a non-empty relative path")
        if value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", value):
            raise ValueError("absolute storage paths are forbidden")
        # Treat both separators as separators.  This prevents a Windows-style
        # traversal from becoming valid when the runner is deployed on Linux.
        parts = re.split(r"[/\\]", value)
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("storage path contains an empty, dot, or parent component")
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", part) for part in parts):
            raise ValueError("storage path contains an unsupported component")
        if len(parts) > MAX_STORAGE_COMPONENTS:
            raise ValueError("storage path has too many components")
        return self

    def parts(self) -> tuple[str, ...]:
        return tuple(re.split(r"[/\\]", self.root))

    def __str__(self) -> str:
        return "/".join(self.parts())


class StorageRef(RunnerModel):
    """A typed reference to a file or directory inside a configured root."""

    schema_version: Literal["runner-storage-ref/v1"] = "runner-storage-ref/v1"
    root: StorageRoot
    relative_path: RelativeStoragePath


class SecretRef(RootModel[str]):
    """Logical systemd credential name; never a credential value or path."""

    model_config = ConfigDict(frozen=True)

    @model_validator(mode="after")
    def validate_name(self) -> Self:
        if re.fullmatch(r"[a-z][a-z0-9-]{2,63}", self.root) is None:
            raise ValueError("secret reference must be a logical kebab-case name")
        return self

    def __str__(self) -> str:
        return self.root


class VllmParameters(RunnerModel):
    """The bounded vLLM parameter surface compiled by the deployment capability."""

    schema_version: Literal["runner-vllm-parameters/v1"] = "runner-vllm-parameters/v1"
    tensor_parallel_size: Literal[1] = 1
    max_model_len: int = Field(ge=1, le=50_000_000)
    gpu_memory_utilization: float = Field(ge=0.80, le=0.94)
    max_num_seqs: Literal[4, 8, 16, 32]
    max_num_batched_tokens: Literal[2048, 4096, 8192, 16384]
    enable_chunked_prefill: bool
    trust_remote_code: Literal[False] = False

    @model_validator(mode="after")
    def validate_scheduler_batch(self) -> Self:
        if not self.enable_chunked_prefill and self.max_num_batched_tokens < self.max_model_len:
            raise ValueError(
                "max_num_batched_tokens must cover max_model_len when chunked prefill is disabled"
            )
        return self


class EnvironmentInspectPayload(RunnerModel):
    schema_version: Literal["runner-environment-inspect/v1"] = "runner-environment-inspect/v1"
    scope: Literal["mvp_full"] = "mvp_full"
    include_runtime_probe: bool = True


class DeploymentStartPayload(RunnerModel):
    schema_version: Literal["runner-deployment-start/v1"] = "runner-deployment-start/v1"
    deployment_id: str = Field(min_length=3, max_length=128, pattern=r"^deployment_[0-9a-f]{32}$")
    worker_id: str = Field(min_length=3, max_length=128, pattern=r"^worker_[0-9a-f]{32}$")
    image: ImageDigest
    model_repository: RepositoryId
    model_revision: ModelRevision
    model_cache: StorageRef
    parameters: VllmParameters
    pid_limit: int = Field(ge=64, le=65_536)
    startup_timeout_seconds: int = Field(ge=1, le=1_800)
    task_timeout_seconds: int = Field(ge=1, le=1_800)
    max_disk_growth_bytes: int = Field(ge=1, le=20_000_000_000)
    model_token: SecretRef | None = None

    @model_validator(mode="after")
    def validate_roots(self) -> Self:
        if self.model_cache.root is not StorageRoot.MODEL_CACHE:
            raise ValueError("deployment model cache must use the model-cache root")
        if self.startup_timeout_seconds > self.task_timeout_seconds:
            raise ValueError("startup timeout cannot exceed task timeout")
        return self


class DeploymentRefPayload(RunnerModel):
    schema_version: Literal["runner-deployment-ref/v1"] = "runner-deployment-ref/v1"
    deployment_id: str = Field(min_length=3, max_length=128, pattern=r"^deployment_[0-9a-f]{32}$")


class RunnerArtifactInput(RunnerModel):
    """Immutable local Artifact input without a caller-supplied host path."""

    schema_version: Literal["runner-artifact-input/v1"] = "runner-artifact-input/v1"
    artifact_id: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^artifact_[0-9a-f]{32}$",
    )
    sha256: Sha256Digest
    content_type: Literal["application/json"] = "application/json"
    size_bytes: int = Field(ge=1, le=100_000_000)
    storage: StorageRef

    @model_validator(mode="after")
    def validate_storage_root(self) -> Self:
        if self.storage.root is not StorageRoot.OUTPUT:
            raise ValueError("runner input Artifacts must use the output root")
        return self


class BenchmarkStartPayload(RunnerModel):
    schema_version: Literal["runner-benchmark-start/v1"] = "runner-benchmark-start/v1"
    benchmark_id: str = Field(min_length=3, max_length=128, pattern=r"^benchmark_[0-9a-f]{32}$")
    deployment_id: str = Field(min_length=3, max_length=128, pattern=r"^deployment_[0-9a-f]{32}$")
    compiled_spec_artifact: RunnerArtifactInput
    max_duration_seconds: int = Field(ge=1, le=1_800)
    max_requests: int = Field(ge=1, le=10_000)
    max_input_tokens: int = Field(ge=1, le=50_000_000)
    max_output_tokens: int = Field(ge=1, le=50_000_000)
    cpu_millis: int = Field(ge=100, le=32_000)
    max_memory_bytes: int = Field(ge=268_435_456, le=68_719_476_736)
    pid_limit: int = Field(ge=16, le=4_096)
    max_disk_growth_bytes: int = Field(ge=1, le=5_000_000_000)

    @model_validator(mode="after")
    def validate_output_root(self) -> Self:
        if self.compiled_spec_artifact.storage.root is not StorageRoot.OUTPUT:
            raise ValueError("compiled benchmark specs must use the output root")
        return self


class HuggingFacePlannerModelRef(RunnerModel):
    type: Literal["huggingface"] = "huggingface"
    repository_id: RepositoryId
    revision: ModelRevision
    config_artifact: RunnerArtifactInput


class ArtifactPlannerModelRef(RunnerModel):
    type: Literal["artifact"] = "artifact"
    model_artifact: RunnerArtifactInput
    revision: ModelRevision
    config_artifact: RunnerArtifactInput


PlannerModelRef = Annotated[
    HuggingFacePlannerModelRef | ArtifactPlannerModelRef,
    Field(discriminator="type"),
]


class PlannerRuntimeBudget(RunnerModel):
    schema_version: Literal["runner-planner-budget/v1"] = "runner-planner-budget/v1"
    max_duration_seconds: int = Field(ge=1, le=600)
    cpu_millis: int = Field(ge=100, le=32_000)
    max_memory_bytes: int = Field(ge=268_435_456, le=68_719_476_736)
    pid_limit: int = Field(ge=16, le=4_096)
    max_disk_growth_bytes: int = Field(ge=1, le=5_000_000_000)


class CapacityPlannerStartPayload(RunnerModel):
    schema_version: Literal["runner-capacity-planner-start/v1"] = "runner-capacity-planner-start/v1"
    job_id: str = Field(min_length=3, max_length=128, pattern=r"^job_[0-9a-f]{32}$")
    model_ref: PlannerModelRef
    hardware_passport_artifact: RunnerArtifactInput
    tensor_parallel_size: Literal[1] = 1
    budget: PlannerRuntimeBudget


class CapacityPlannerJobRefPayload(RunnerModel):
    schema_version: Literal["runner-capacity-planner-job-ref/v1"] = (
        "runner-capacity-planner-job-ref/v1"
    )
    job_id: str = Field(min_length=3, max_length=128, pattern=r"^job_[0-9a-f]{32}$")


class JobRefPayload(RunnerModel):
    schema_version: Literal["runner-job-ref/v1"] = "runner-job-ref/v1"
    job_id: str = Field(min_length=3, max_length=128, pattern=r"^job_[0-9a-f]{32}$")


class GpuLeasePayload(RunnerModel):
    schema_version: Literal["runner-gpu-lease/v1"] = "runner-gpu-lease/v1"
    lease_id: str = Field(min_length=8, max_length=128, pattern=r"^gpu-lease_[a-z0-9-]+$")
    owner_id: str = Field(min_length=3, max_length=128, pattern=r"^worker_[0-9a-f]{32}$")
    gpu_index: Literal[0] = 0
    fencing_token: int | None = Field(default=None, ge=1)
    lease_duration_seconds: int = Field(default=60, ge=1, le=3_600)


class TemporaryCleanupPayload(RunnerModel):
    schema_version: Literal["runner-temporary-cleanup/v1"] = "runner-temporary-cleanup/v1"
    temporary_ref: StorageRef

    @model_validator(mode="after")
    def validate_root(self) -> Self:
        if self.temporary_ref.root is not StorageRoot.TEMPORARY:
            raise ValueError("temporary cleanup can only target the temporary root")
        return self


class RunnerRequestBase(RunnerModel):
    schema_version: Literal["runner-request/v1"] = "runner-request/v1"
    request_id: RunnerRequestId
    idempotency_key: Sha256Digest
    actor: Literal["autopilot-api", "autopilot-worker"]
    plan_id: str = Field(min_length=3, max_length=128, pattern=r"^plan_[0-9a-f]{32}$")
    plan_hash: Sha256Digest


class InspectEnvironmentRequest(RunnerRequestBase):
    action: Literal[RunnerAction.INSPECT_ENVIRONMENT] = RunnerAction.INSPECT_ENVIRONMENT
    payload: EnvironmentInspectPayload


class StartDeploymentRequest(RunnerRequestBase):
    action: Literal[RunnerAction.START_DEPLOYMENT] = RunnerAction.START_DEPLOYMENT
    payload: DeploymentStartPayload


class StopDeploymentRequest(RunnerRequestBase):
    action: Literal[RunnerAction.STOP_DEPLOYMENT] = RunnerAction.STOP_DEPLOYMENT
    payload: DeploymentRefPayload


class DeploymentStatusRequest(RunnerRequestBase):
    action: Literal[RunnerAction.GET_DEPLOYMENT_STATUS] = RunnerAction.GET_DEPLOYMENT_STATUS
    payload: DeploymentRefPayload


class StartBenchmarkRequest(RunnerRequestBase):
    action: Literal[RunnerAction.START_BENCHMARK] = RunnerAction.START_BENCHMARK
    payload: BenchmarkStartPayload


class CancelBenchmarkRequest(RunnerRequestBase):
    action: Literal[RunnerAction.CANCEL_BENCHMARK] = RunnerAction.CANCEL_BENCHMARK
    payload: JobRefPayload


class JobStatusRequest(RunnerRequestBase):
    action: Literal[RunnerAction.GET_JOB_STATUS] = RunnerAction.GET_JOB_STATUS
    payload: JobRefPayload


class JobArtifactsRequest(RunnerRequestBase):
    action: Literal[RunnerAction.GET_JOB_ARTIFACTS] = RunnerAction.GET_JOB_ARTIFACTS
    payload: JobRefPayload


class StartCapacityPlannerRequest(RunnerRequestBase):
    action: Literal[RunnerAction.START_CAPACITY_PLANNER] = RunnerAction.START_CAPACITY_PLANNER
    payload: CapacityPlannerStartPayload


class CapacityPlannerStatusRequest(RunnerRequestBase):
    action: Literal[RunnerAction.GET_CAPACITY_PLANNER_STATUS] = (
        RunnerAction.GET_CAPACITY_PLANNER_STATUS
    )
    payload: CapacityPlannerJobRefPayload


class CancelCapacityPlannerRequest(RunnerRequestBase):
    action: Literal[RunnerAction.CANCEL_CAPACITY_PLANNER] = RunnerAction.CANCEL_CAPACITY_PLANNER
    payload: CapacityPlannerJobRefPayload


class CapacityPlannerArtifactsRequest(RunnerRequestBase):
    action: Literal[RunnerAction.GET_CAPACITY_PLANNER_ARTIFACTS] = (
        RunnerAction.GET_CAPACITY_PLANNER_ARTIFACTS
    )
    payload: CapacityPlannerJobRefPayload


class AcquireGpuLeaseRequest(RunnerRequestBase):
    action: Literal[RunnerAction.ACQUIRE_GPU_LEASE] = RunnerAction.ACQUIRE_GPU_LEASE
    payload: GpuLeasePayload


class HeartbeatGpuLeaseRequest(RunnerRequestBase):
    action: Literal[RunnerAction.HEARTBEAT_GPU_LEASE] = RunnerAction.HEARTBEAT_GPU_LEASE
    payload: GpuLeasePayload


class ReleaseGpuLeaseRequest(RunnerRequestBase):
    action: Literal[RunnerAction.RELEASE_GPU_LEASE] = RunnerAction.RELEASE_GPU_LEASE
    payload: GpuLeasePayload


class CleanupTemporaryRequest(RunnerRequestBase):
    action: Literal[RunnerAction.CLEANUP_TEMPORARY] = RunnerAction.CLEANUP_TEMPORARY
    payload: TemporaryCleanupPayload


RunnerRequest = Annotated[
    InspectEnvironmentRequest
    | StartDeploymentRequest
    | StopDeploymentRequest
    | DeploymentStatusRequest
    | StartBenchmarkRequest
    | CancelBenchmarkRequest
    | JobStatusRequest
    | JobArtifactsRequest
    | StartCapacityPlannerRequest
    | CapacityPlannerStatusRequest
    | CancelCapacityPlannerRequest
    | CapacityPlannerArtifactsRequest
    | AcquireGpuLeaseRequest
    | HeartbeatGpuLeaseRequest
    | ReleaseGpuLeaseRequest
    | CleanupTemporaryRequest,
    Field(discriminator="action"),
]


class RunnerError(RunnerModel):
    schema_version: Literal["runner-error/v1"] = "runner-error/v1"
    code: str = Field(min_length=3, max_length=64, pattern=r"^[A-Z][A-Z0-9_]+$")
    message: str = Field(min_length=1, max_length=512)
    retryable: bool = False


class LeaseResponse(RunnerModel):
    schema_version: Literal["runner-lease-response/v1"] = "runner-lease-response/v1"
    lease_id: str
    owner_id: str
    gpu_index: Literal[0] = 0
    fencing_token: int = Field(ge=1)
    heartbeat_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def normalize_utc(self) -> Self:
        if self.heartbeat_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("lease timestamps must be timezone-aware")
        object.__setattr__(self, "heartbeat_at", self.heartbeat_at.astimezone(UTC))
        object.__setattr__(self, "expires_at", self.expires_at.astimezone(UTC))
        return self


class OperationResponse(RunnerModel):
    schema_version: Literal["runner-operation-response/v1"] = "runner-operation-response/v1"
    resource_id: str = Field(min_length=3, max_length=128)
    state: Literal["accepted", "running", "stopped", "cancelled", "succeeded", "failed"]
    lease: LeaseResponse | None = None
    detail: str | None = Field(default=None, max_length=512)


class RunnerResponse(RunnerModel):
    schema_version: Literal["runner-response/v1"] = "runner-response/v1"
    request_id: RunnerRequestId
    accepted: bool
    idempotent_replay: bool = False
    result: OperationResponse | None = None
    error: RunnerError | None = None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.accepted == (self.error is not None):
            raise ValueError(
                "accepted responses require a result and denied responses require an error"
            )
        if self.accepted and self.result is None:
            raise ValueError("accepted responses require a result")
        if not self.accepted and self.result is not None:
            raise ValueError("denied responses cannot include a result")
        return self


def utc_now() -> datetime:
    """Return an aware UTC timestamp without depending on system local time."""

    return datetime.now(UTC)
