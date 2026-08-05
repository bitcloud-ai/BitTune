import hashlib

import pytest

from autopilot.capabilities.deployment.domain.models import (
    DeploymentExecutionSpecification,
    VllmVersionProfile,
)
from autopilot.domain.artifacts import ArtifactProducer, ArtifactRef
from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.candidates import DeploymentCandidate, VllmTuningSpec
from autopilot.domain.enums import Confidence
from autopilot.domain.hashing import compute_content_hash
from autopilot.domain.identifiers import (
    ArtifactId,
    CandidateId,
    HardwarePassportId,
    ImageDigest,
    ModelProfileId,
    ModelRevision,
    Sha256Digest,
)
from autopilot.domain.models import HuggingFaceModelRef
from autopilot.domain.provenance import EstimatedProvenance
from autopilot.domain.workloads import (
    SamplingSpec,
    SyntheticFixedDataset,
    TokenizerRef,
    WorkloadSpec,
)


@pytest.fixture
def deployment_specification() -> DeploymentExecutionSpecification:
    raw = b"capacity-estimate"
    artifact = ArtifactRef(
        artifact_id=ArtifactId(root=f"artifact_{'4' * 32}"),
        sha256=Sha256Digest(root=f"sha256:{hashlib.sha256(raw).hexdigest()}"),
        content_type="application/json",
        size_bytes=len(raw),
        producer=ArtifactProducer(component="capacity", version="1.0.0"),
    )
    revision = ModelRevision(root="b" * 40)
    model_ref = HuggingFaceModelRef(repository_id="Qwen/Qwen3-8B", revision=revision)
    workload = WorkloadSpec(
        dataset=SyntheticFixedDataset(dataset_id="medium-v1"),
        tokenizer=TokenizerRef(repository_id="Qwen/Qwen3-8B", revision=revision),
        prompt_tokens=2_048,
        output_tokens=512,
        stream=True,
        ignore_eos=True,
        sampling=SamplingSpec(seed=20_260_805),
    )
    image = ImageDigest(root=f"vllm/vllm-openai@sha256:{'a' * 64}")
    candidate = DeploymentCandidate(
        candidate_id=CandidateId(root=f"cand_{'1' * 32}"),
        profile="balanced",
        hardware_passport_id=HardwarePassportId(root=f"env_{'2' * 32}"),
        hardware_passport_hash=Sha256Digest(root=f"sha256:{'c' * 64}"),
        model_profile_id=ModelProfileId(root=f"model_{'3' * 32}"),
        model_ref=model_ref,
        engine_image=image,
        engine_version="0.26.0-test",
        adapter_version="deployment-adapter-test-v1",
        workload_hash=compute_content_hash(workload),
        parameters=VllmTuningSpec(
            max_model_len=8_192,
            gpu_memory_utilization=0.90,
            max_num_seqs=8,
            max_num_batched_tokens=4_096,
            enable_chunked_prefill=True,
        ),
        estimation=EstimatedProvenance(
            provider="llm-d-planner",
            provider_version="test-commit",
            adapter_version="capacity-adapter-test-v1",
            confidence=Confidence.MEDIUM,
            calculation_artifact=artifact,
        ),
    )
    return DeploymentExecutionSpecification(
        provider_version="0.26.0-test",
        adapter_version="deployment-adapter-test-v1",
        provider_profile_version="vllm-rtx5090-test-v1",
        budget=ExecutionBudget(
            max_duration_seconds=900,
            max_requests=1,
            max_input_tokens=1,
            max_output_tokens=1,
            max_disk_growth_bytes=10_000_000_000,
        ),
        candidate=candidate,
        workload=workload,
    )


@pytest.fixture
def vllm_profile() -> VllmVersionProfile:
    return VllmVersionProfile(
        profile_version="vllm-rtx5090-test-v1",
        provider_version="0.26.0-test",
        adapter_version="deployment-adapter-test-v1",
        engine_image=ImageDigest(root=f"vllm/vllm-openai@sha256:{'a' * 64}"),
        rtx_5090_verified=True,
        max_model_len_upper_bound=32_768,
        gpu_memory_utilization_min=0.80,
        gpu_memory_utilization_max=0.94,
        supported_max_num_seqs=(4, 8, 16, 32),
        supported_max_num_batched_tokens=(2_048, 4_096, 8_192, 16_384),
        supports_chunked_prefill=True,
        container_port=8_000,
        pid_limit=1_024,
        startup_timeout_seconds=600,
        max_task_timeout_seconds=1_800,
        health_check_timeout_seconds=30,
    )
