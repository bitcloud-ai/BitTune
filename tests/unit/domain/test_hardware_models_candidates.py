from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from autopilot.domain.candidates import (
    DeploymentCandidate,
    VllmTuningSpec,
    validate_candidate_workload,
)
from autopilot.domain.hardware import (
    EnvironmentIssue,
    HardwarePassport,
    HostCpu,
    HostMemory,
    HostOs,
    RuntimeVersions,
    StorageVolume,
)
from autopilot.domain.identifiers import (
    CandidateId,
    HardwarePassportId,
    ImageDigest,
    ModelProfileId,
    Sha256Digest,
)
from autopilot.domain.models import HuggingFaceModelRef
from autopilot.domain.provenance import EstimatedProvenance, MeasuredProvenance
from autopilot.domain.workloads import WorkloadSpec

SHA = "a" * 64


def test_host_capacity_rejects_available_bytes_above_total() -> None:
    with pytest.raises(ValidationError, match="cannot exceed"):
        HostMemory(total_bytes=64, available_bytes=65)


def test_hardware_capability_requires_one_gpu(
    measured_provenance: MeasuredProvenance,
) -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        HardwarePassport(
            hardware_passport_id=HardwarePassportId.new(),
            captured_at=datetime.now(UTC),
            os=HostOs(name="Ubuntu", version="24.04", kernel="6.8", architecture="x86_64"),
            cpu=HostCpu(model="test", logical_cores=16),
            memory=HostMemory(total_bytes=128, available_bytes=64),
            storage=(StorageVolume(volume_id="models", total_bytes=1_000, available_bytes=500),),
            accelerators=(),
            runtime=RuntimeVersions(
                driver_version=None,
                docker_version=None,
                compose_version=None,
                nvidia_container_toolkit_version=None,
                gpu_container_probe="failed",
            ),
            capabilities=("single_nvidia_gpu",),
            provenance=measured_provenance,
        )


def test_docker_gpu_capability_requires_passed_probe(
    measured_provenance: MeasuredProvenance,
) -> None:
    common = {
        "hardware_passport_id": HardwarePassportId.new(),
        "captured_at": datetime.now(UTC),
        "os": HostOs(name="Ubuntu", version="24.04", kernel="6.8", architecture="x86_64"),
        "cpu": HostCpu(model="test", logical_cores=16),
        "memory": HostMemory(total_bytes=128, available_bytes=64),
        "storage": (StorageVolume(volume_id="models", total_bytes=1_000, available_bytes=500),),
        "accelerators": (
            {
                "name": "NVIDIA GeForce RTX 5090",
                "uuid": "GPU-1234567890abcdef",
                "memory_total_bytes": 32_000_000_000,
                "memory_free_bytes": 31_000_000_000,
                "temperature_celsius": 30,
                "utilization_percent": 0,
            },
        ),
        "runtime": RuntimeVersions(
            driver_version="test",
            docker_version="test",
            compose_version="test",
            nvidia_container_toolkit_version="test",
            gpu_container_probe="failed",
        ),
        "provenance": measured_provenance,
    }

    with pytest.raises(ValidationError, match="passed probe"):
        HardwarePassport(
            capabilities=("single_nvidia_gpu", "docker_gpu"),
            **common,
        )

    with pytest.raises(ValidationError, match="without blockers"):
        HardwarePassport(
            runtime=common["runtime"].model_copy(update={"gpu_container_probe": "passed"}),
            capabilities=(
                "single_nvidia_gpu",
                "docker_gpu",
                "vllm_single_gpu_candidate",
            ),
            issues=(EnvironmentIssue(code="DISK_FULL", severity="blocker", message="disk full"),),
            **{key: value for key, value in common.items() if key != "runtime"},
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tensor_parallel_size", 2),
        ("gpu_memory_utilization", 0.95),
        ("max_num_seqs", 64),
        ("max_num_batched_tokens", 1024),
        ("trust_remote_code", True),
    ],
)
def test_vllm_tuning_spec_enforces_mvp_boundary(field: str, value: object) -> None:
    payload: dict[str, object] = {
        "tensor_parallel_size": 1,
        "max_model_len": 8_192,
        "gpu_memory_utilization": 0.90,
        "max_num_seqs": 8,
        "max_num_batched_tokens": 4_096,
        "enable_chunked_prefill": True,
        "trust_remote_code": False,
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        VllmTuningSpec.model_validate(payload)


def test_vllm_tuning_rejects_full_prefill_larger_than_scheduler_batch() -> None:
    with pytest.raises(ValidationError, match="chunked prefill is disabled"):
        VllmTuningSpec(
            max_model_len=8_192,
            gpu_memory_utilization=0.9,
            max_num_seqs=8,
            max_num_batched_tokens=2_048,
            enable_chunked_prefill=False,
        )


def test_candidate_rejects_workload_longer_than_model_context(
    estimated_provenance: EstimatedProvenance,
    model_ref: HuggingFaceModelRef,
    workload: WorkloadSpec,
) -> None:
    candidate = DeploymentCandidate(
        candidate_id=CandidateId.new(),
        profile="balanced",
        hardware_passport_id=HardwarePassportId.new(),
        hardware_passport_hash=Sha256Digest(root=f"sha256:{SHA}"),
        model_profile_id=ModelProfileId.new(),
        model_ref=model_ref,
        engine_image=ImageDigest(root=f"vllm/vllm-openai@sha256:{SHA}"),
        engine_version="0.26.0",
        adapter_version="1.0.0",
        workload_hash=Sha256Digest(root=f"sha256:{SHA}"),
        parameters=VllmTuningSpec(
            max_model_len=2_500,
            gpu_memory_utilization=0.90,
            max_num_seqs=8,
            max_num_batched_tokens=4_096,
            enable_chunked_prefill=True,
        ),
        estimation=estimated_provenance,
    )

    with pytest.raises(ValueError, match="max_model_len"):
        validate_candidate_workload(candidate, workload)
