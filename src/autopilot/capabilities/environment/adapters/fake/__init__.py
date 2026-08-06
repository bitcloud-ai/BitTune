"""Deterministic environment fakes used by workflow and contract tests."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from pydantic import BaseModel

from autopilot.capabilities.environment.application.service import EnvironmentInspectionService
from autopilot.capabilities.environment.domain.models import (
    EnvironmentVersionProfile,
    GpuSnapshot,
    HostSnapshot,
)
from autopilot.domain.artifacts import ArtifactProducer, ArtifactRef
from autopilot.domain.hardware import HostCpu, HostMemory, HostOs, RuntimeVersions, StorageVolume
from autopilot.domain.identifiers import ArtifactId, Sha256Digest

FAKE_ENVIRONMENT_PROFILE = EnvironmentVersionProfile(
    profile_version="fake-rtx5090-environment-v1",
    provider_version="fake-nvml-1.0.0",
    adapter_version="fake-environment-adapter-v1",
    rtx_5090_verified=True,
    expected_memory_total_bytes=34_359_738_368,
    memory_tolerance_bytes=0,
)


class FakeNvmlCollector:
    """Return one idle 32 GiB RTX 5090 without touching a GPU."""

    @property
    def driver_version(self) -> str:
        return "fake-driver-1.0.0"

    def collect_gpu_zero(self) -> GpuSnapshot:
        return GpuSnapshot(
            name="NVIDIA GeForce RTX 5090",
            uuid="GPU-aaaaaaaaaaaaaaaa",
            memory_total_bytes=34_359_738_368,
            memory_free_bytes=33_285_996_544,
            temperature_celsius=42.0,
            utilization_percent=0.0,
            power_watts=35.0,
        )


class FakeLinuxHostCollector:
    """Return a fixed Linux host snapshot."""

    def collect(self, *, gpu: GpuSnapshot, driver_version: str) -> HostSnapshot:
        return HostSnapshot(
            os=HostOs(
                name="Ubuntu",
                version="24.04",
                kernel="6.8.0-31-generic",
                architecture="x86_64",
            ),
            cpu=HostCpu(model="Fake 32 Core CPU", logical_cores=32),
            memory=HostMemory(
                total_bytes=137_438_953_472,
                available_bytes=103_079_215_104,
            ),
            storage=(
                StorageVolume(
                    volume_id="model-cache",
                    total_bytes=2_199_023_255_552,
                    available_bytes=1_649_267_441_664,
                ),
            ),
            runtime=RuntimeVersions(
                driver_version=driver_version,
                docker_version="fake-docker-1.0.0",
                compose_version="fake-compose-1.0.0",
                nvidia_container_toolkit_version="fake-toolkit-1.0.0",
                gpu_container_probe="passed",
            ),
            gpu=gpu,
            captured_at=datetime(2026, 8, 5, tzinfo=UTC),
        )


class InMemoryEnvironmentArtifactSink:
    """Create content-bound Artifact refs without filesystem I/O."""

    def write_environment_artifact(
        self,
        payload: BaseModel,
        *,
        adapter_version: str,
    ) -> ArtifactRef:
        data = payload.model_dump_json().encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        return ArtifactRef(
            artifact_id=ArtifactId(root=f"artifact_{digest[:32]}"),
            sha256=Sha256Digest(root=f"sha256:{digest}"),
            content_type="application/json",
            size_bytes=len(data),
            producer=ArtifactProducer(component="environment", version=adapter_version),
        )


class FakeEnvironmentAdapter(EnvironmentInspectionService):
    """Complete deterministic Environment Adapter for non-GPU tests."""

    def __init__(self) -> None:
        super().__init__(
            profile=FAKE_ENVIRONMENT_PROFILE,
            nvml=FakeNvmlCollector(),
            host=FakeLinuxHostCollector(),
            artifacts=InMemoryEnvironmentArtifactSink(),
        )
