"""Versioned environment inspection contracts and normalized snapshots."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from autopilot.domain.base import NonEmptyStr, StrictModel, UtcDatetime
from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.enums import Confidence
from autopilot.domain.hardware import (
    GpuProcess,
    HardwarePassport,
    HostCpu,
    HostMemory,
    HostOs,
    RuntimeVersions,
    StorageVolume,
)
from autopilot.domain.identifiers import HardwarePassportId
from autopilot.domain.plans import ExecutionSpecification

INVALID_GPU_MEMORY = "GPU snapshot free memory cannot exceed total memory"
INVALID_GPU_INDEX = "the MVP environment snapshot must describe GPU 0"
INVALID_RESULT_ID = "environment result ID must match its Hardware Passport"
INVALID_EXECUTION_BINDING = "environment execution profile must match its inspection specification"


class EnvironmentVersionProfile(StrictModel):
    """A Phase-0 verified NVML/host collector combination."""

    schema_version: Literal["environment-version-profile/v1"] = "environment-version-profile/v1"
    profile_version: NonEmptyStr
    provider_version: NonEmptyStr
    adapter_version: NonEmptyStr
    rtx_5090_verified: Literal[True]
    expected_gpu_name: Literal["NVIDIA GeForce RTX 5090"] = "NVIDIA GeForce RTX 5090"
    expected_memory_total_bytes: int = Field(ge=30_000_000_000, le=40_000_000_000)
    memory_tolerance_bytes: int = Field(ge=0, le=5_000_000_000)
    gpu_index: Literal[0] = 0


class EnvironmentInspectionSpecification(StrictModel):
    """Immutable, read-only inspection input."""

    schema_version: Literal["environment-inspection-specification/v1"] = (
        "environment-inspection-specification/v1"
    )
    provider: Literal["nvml"] = "nvml"
    provider_version: NonEmptyStr
    adapter_version: NonEmptyStr
    provider_profile_version: NonEmptyStr
    scope: Literal["mvp_full"] = "mvp_full"
    include_runtime_probe: bool = True


class EnvironmentExecutionSpecification(ExecutionSpecification):
    """Persisted execution material with the Experiment budget bound server-side."""

    schema_version: Literal["environment-execution-specification/v1"] = (
        "environment-execution-specification/v1"
    )
    provider: Literal["nvml"] = "nvml"
    budget: ExecutionBudget
    inspection: EnvironmentInspectionSpecification

    @model_validator(mode="after")
    def validate_profile_binding(self) -> Self:
        if (
            self.provider_version != self.inspection.provider_version
            or self.adapter_version != self.inspection.adapter_version
            or self.provider_profile_version != self.inspection.provider_profile_version
        ):
            raise ValueError(INVALID_EXECUTION_BINDING)
        return self


class GpuSnapshot(StrictModel):
    """Raw, normalized read-only GPU fields collected from NVML."""

    index: Literal[0] = 0
    name: NonEmptyStr
    uuid: str = Field(pattern=r"^GPU-[A-Fa-f0-9-]{16,64}$")
    memory_total_bytes: int = Field(ge=1, le=1_000_000_000_000)
    memory_free_bytes: int = Field(ge=0, le=1_000_000_000_000)
    temperature_celsius: float = Field(ge=-100, le=200)
    utilization_percent: float = Field(ge=0, le=100)
    power_watts: float | None = Field(default=None, ge=0, le=10_000)
    processes: tuple[GpuProcess, ...] = Field(default=(), max_length=4_096)

    @model_validator(mode="after")
    def validate_memory(self) -> Self:
        if self.memory_free_bytes > self.memory_total_bytes:
            raise ValueError(INVALID_GPU_MEMORY)
        return self


class HostSnapshot(StrictModel):
    """Host fields supplied by deterministic Linux collectors."""

    os: HostOs
    cpu: HostCpu
    memory: HostMemory
    storage: tuple[StorageVolume, ...] = Field(min_length=1, max_length=128)
    runtime: RuntimeVersions
    gpu: GpuSnapshot
    captured_at: UtcDatetime

    @model_validator(mode="after")
    def validate_gpu_index(self) -> Self:
        if self.gpu.index != 0:
            raise ValueError(INVALID_GPU_INDEX)
        return self


class EnvironmentInspectionResult(StrictModel):
    """Persistable result returned by an environment adapter."""

    schema_version: Literal["environment-inspection-result/v1"] = "environment-inspection-result/v1"
    hardware_passport: HardwarePassport
    hardware_passport_id: HardwarePassportId
    confidence: Confidence = Confidence.HIGH

    @model_validator(mode="after")
    def validate_id_binding(self) -> Self:
        if self.hardware_passport.hardware_passport_id != self.hardware_passport_id:
            raise ValueError(INVALID_RESULT_ID)
        return self
