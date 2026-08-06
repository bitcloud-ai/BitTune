"""Pure Hardware Passport construction from validated collector snapshots."""

from autopilot.capabilities.environment.domain.models import EnvironmentVersionProfile, HostSnapshot
from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.hardware import EnvironmentIssue, HardwarePassport, NvidiaAccelerator
from autopilot.domain.identifiers import HardwarePassportId
from autopilot.domain.provenance import MeasuredProvenance


def build_hardware_passport(
    snapshot: HostSnapshot,
    profile: EnvironmentVersionProfile,
    raw_artifact: ArtifactRef,
    *,
    hardware_passport_id: HardwarePassportId,
) -> HardwarePassport:
    """Normalize an immutable host snapshot and derive safe capabilities."""
    issues: list[EnvironmentIssue] = []
    if snapshot.gpu.processes:
        issues.append(
            EnvironmentIssue(
                code="GPU_ZERO_BUSY",
                severity="blocker",
                message="GPU 0 has active compute processes and is not available for exclusive use",
            )
        )
    if snapshot.runtime.gpu_container_probe != "passed":
        issues.append(
            EnvironmentIssue(
                code="DOCKER_GPU_PROBE_NOT_PASSED",
                severity="blocker",
                message="the read-only Docker GPU probe did not pass",
            )
        )

    capabilities: list[str] = ["single_nvidia_gpu"]
    if snapshot.runtime.gpu_container_probe == "passed":
        capabilities.append("docker_gpu")
    if not issues and snapshot.runtime.gpu_container_probe == "passed":
        capabilities.append("vllm_single_gpu_candidate")

    gpu = snapshot.gpu
    return HardwarePassport(
        hardware_passport_id=hardware_passport_id,
        captured_at=snapshot.captured_at,
        os=snapshot.os,
        cpu=snapshot.cpu,
        memory=snapshot.memory,
        storage=snapshot.storage,
        accelerators=(
            NvidiaAccelerator(
                name=gpu.name,
                uuid=gpu.uuid,
                memory_total_bytes=gpu.memory_total_bytes,
                memory_free_bytes=gpu.memory_free_bytes,
                temperature_celsius=gpu.temperature_celsius,
                utilization_percent=gpu.utilization_percent,
                power_watts=gpu.power_watts,
                processes=gpu.processes,
            ),
        ),
        runtime=snapshot.runtime,
        capabilities=tuple(capabilities),
        issues=tuple(issues),
        provenance=MeasuredProvenance(
            provider="nvml-host-collector",
            provider_version=profile.provider_version,
            adapter_version=profile.adapter_version,
            raw_artifact=raw_artifact,
        ),
    )
