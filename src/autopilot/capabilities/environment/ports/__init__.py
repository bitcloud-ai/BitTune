"""Ports owned by the environment capability."""

from typing import Protocol

from pydantic import BaseModel

from autopilot.capabilities.environment.domain.models import (
    EnvironmentInspectionResult,
    EnvironmentInspectionSpecification,
    EnvironmentVersionProfile,
    GpuSnapshot,
    HostSnapshot,
)
from autopilot.domain.artifacts import ArtifactRef


class EnvironmentArtifactSink(Protocol):
    """Persist a validated environment payload and return its immutable reference."""

    def write_environment_artifact(
        self,
        payload: BaseModel,
        *,
        adapter_version: str,
    ) -> ArtifactRef: ...


class NvmlCollector(Protocol):
    """Read the stable NVML field subset used by the MVP."""

    @property
    def driver_version(self) -> str: ...

    def collect_gpu_zero(self) -> GpuSnapshot: ...


class LinuxHostCollector(Protocol):
    """Collect deterministic Linux host fields without executing a shell."""

    def collect(self, *, gpu: GpuSnapshot, driver_version: str) -> HostSnapshot: ...


class EnvironmentAdapter(Protocol):
    """Provider lifecycle exposed to the environment application service."""

    @property
    def profile(self) -> EnvironmentVersionProfile: ...

    def validate(self, specification: EnvironmentInspectionSpecification) -> None: ...

    def inspect(
        self,
        specification: EnvironmentInspectionSpecification,
    ) -> EnvironmentInspectionResult: ...
