"""Normalized read-only hardware and runtime passport."""

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from autopilot.domain.base import LongText, NonEmptyStr, StrictModel, UtcDatetime
from autopilot.domain.identifiers import HardwarePassportId
from autopilot.domain.provenance import MeasuredProvenance

GpuUuid = Annotated[str, StringConstraints(pattern=r"^GPU-[A-Fa-f0-9-]{16,64}$")]
IssueCode = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{2,127}$")]
AVAILABLE_EXCEEDS_TOTAL = "available capacity cannot exceed total capacity"
FREE_EXCEEDS_TOTAL = "free GPU memory cannot exceed total GPU memory"
DUPLICATE_CAPABILITY = "hardware capabilities must be unique"
GPU_CAPABILITY_MISMATCH = "single-GPU capabilities require exactly one NVIDIA accelerator"
DOCKER_GPU_CAPABILITY_MISMATCH = "Docker GPU capability requires a passed probe and single GPU"
VLLM_CAPABILITY_MISMATCH = "vLLM capability requires Docker GPU readiness without blockers"


class HostOs(StrictModel):
    name: NonEmptyStr
    version: NonEmptyStr
    kernel: NonEmptyStr
    architecture: NonEmptyStr


class HostCpu(StrictModel):
    model: NonEmptyStr
    logical_cores: int = Field(ge=1, le=65_536)


class HostMemory(StrictModel):
    total_bytes: int = Field(ge=1, le=10_000_000_000_000)
    available_bytes: int = Field(ge=0, le=10_000_000_000_000)

    @model_validator(mode="after")
    def validate_available_memory(self) -> Self:
        if self.available_bytes > self.total_bytes:
            raise ValueError(AVAILABLE_EXCEEDS_TOTAL)
        return self


class StorageVolume(StrictModel):
    volume_id: NonEmptyStr
    total_bytes: int = Field(ge=1, le=10_000_000_000_000_000)
    available_bytes: int = Field(ge=0, le=10_000_000_000_000_000)

    @model_validator(mode="after")
    def validate_available_storage(self) -> Self:
        if self.available_bytes > self.total_bytes:
            raise ValueError(AVAILABLE_EXCEEDS_TOTAL)
        return self


class GpuProcess(StrictModel):
    process_id: int = Field(ge=1, le=4_294_967_295)
    used_memory_bytes: int = Field(ge=0, le=1_000_000_000_000)


class NvidiaAccelerator(StrictModel):
    vendor: Literal["nvidia"] = "nvidia"
    index: Literal[0] = 0
    name: NonEmptyStr
    uuid: GpuUuid
    memory_total_bytes: int = Field(ge=1, le=1_000_000_000_000)
    memory_free_bytes: int = Field(ge=0, le=1_000_000_000_000)
    temperature_celsius: float = Field(ge=-100, le=200)
    utilization_percent: float = Field(ge=0, le=100)
    power_watts: float | None = Field(default=None, ge=0, le=10_000)
    processes: tuple[GpuProcess, ...] = Field(default=(), max_length=4_096)

    @model_validator(mode="after")
    def validate_free_memory(self) -> Self:
        if self.memory_free_bytes > self.memory_total_bytes:
            raise ValueError(FREE_EXCEEDS_TOTAL)
        return self


class RuntimeVersions(StrictModel):
    driver_version: NonEmptyStr | None
    docker_version: NonEmptyStr | None
    compose_version: NonEmptyStr | None
    nvidia_container_toolkit_version: NonEmptyStr | None
    gpu_container_probe: Literal["passed", "failed", "not_run"]


class EnvironmentIssue(StrictModel):
    code: IssueCode
    severity: Literal["warning", "blocker"]
    message: LongText


class HardwarePassport(StrictModel):
    schema_version: Literal["hardware-passport/v1"] = "hardware-passport/v1"
    hardware_passport_id: HardwarePassportId
    captured_at: UtcDatetime
    os: HostOs
    cpu: HostCpu
    memory: HostMemory
    storage: tuple[StorageVolume, ...] = Field(min_length=1, max_length=128)
    accelerators: tuple[NvidiaAccelerator, ...] = Field(default=(), max_length=1)
    runtime: RuntimeVersions
    capabilities: tuple[
        Literal["single_nvidia_gpu", "docker_gpu", "vllm_single_gpu_candidate"], ...
    ] = Field(default=(), max_length=3)
    issues: tuple[EnvironmentIssue, ...] = Field(default=(), max_length=256)
    provenance: MeasuredProvenance

    @model_validator(mode="after")
    def validate_capability_consistency(self) -> Self:
        if len(self.capabilities) != len(set(self.capabilities)):
            raise ValueError(DUPLICATE_CAPABILITY)
        gpu_capabilities = {"single_nvidia_gpu", "docker_gpu", "vllm_single_gpu_candidate"}
        if gpu_capabilities.intersection(self.capabilities) and len(self.accelerators) != 1:
            raise ValueError(GPU_CAPABILITY_MISMATCH)
        capability_set = set(self.capabilities)
        if "docker_gpu" in capability_set and (
            "single_nvidia_gpu" not in capability_set
            or self.runtime.gpu_container_probe != "passed"
        ):
            raise ValueError(DOCKER_GPU_CAPABILITY_MISMATCH)
        has_blocker = any(issue.severity == "blocker" for issue in self.issues)
        if "vllm_single_gpu_candidate" in capability_set and (
            not {"single_nvidia_gpu", "docker_gpu"}.issubset(capability_set) or has_blocker
        ):
            raise ValueError(VLLM_CAPABILITY_MISMATCH)
        return self
