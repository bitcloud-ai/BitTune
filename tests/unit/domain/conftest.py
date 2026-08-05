import hashlib

import pytest

from autopilot.domain.artifacts import ArtifactProducer, ArtifactRef
from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.enums import Confidence
from autopilot.domain.identifiers import ArtifactId, ModelRevision, Sha256Digest
from autopilot.domain.models import HuggingFaceModelRef
from autopilot.domain.provenance import (
    DerivedProvenance,
    EstimatedProvenance,
    MeasuredProvenance,
)
from autopilot.domain.workloads import (
    SamplingSpec,
    SyntheticFixedDataset,
    TokenizerRef,
    WorkloadSpec,
)


@pytest.fixture
def artifact_ref() -> ArtifactRef:
    raw = b"evidence"
    return ArtifactRef(
        artifact_id=ArtifactId.new(),
        sha256=Sha256Digest(root=f"sha256:{hashlib.sha256(raw).hexdigest()}"),
        content_type="application/json",
        size_bytes=len(raw),
        producer=ArtifactProducer(component="unit-test", version="1.0.0"),
    )


@pytest.fixture
def model_revision() -> ModelRevision:
    return ModelRevision(root="1" * 40)


@pytest.fixture
def model_ref(model_revision: ModelRevision) -> HuggingFaceModelRef:
    return HuggingFaceModelRef(repository_id="Qwen/Qwen3-8B", revision=model_revision)


@pytest.fixture
def workload(model_revision: ModelRevision) -> WorkloadSpec:
    return WorkloadSpec(
        dataset=SyntheticFixedDataset(dataset_id="medium-v1"),
        tokenizer=TokenizerRef(repository_id="Qwen/Qwen3-8B", revision=model_revision),
        prompt_tokens=2_048,
        output_tokens=512,
        stream=True,
        ignore_eos=True,
        sampling=SamplingSpec(seed=20_260_805),
    )


@pytest.fixture
def execution_budget() -> ExecutionBudget:
    return ExecutionBudget(
        max_duration_seconds=600,
        max_requests=5_000,
        max_input_tokens=10_000_000,
        max_output_tokens=1_000_000,
        max_disk_growth_bytes=20_000_000_000,
    )


@pytest.fixture
def estimated_provenance(artifact_ref: ArtifactRef) -> EstimatedProvenance:
    return EstimatedProvenance(
        provider="llm-d-planner",
        provider_version="commit-123",
        adapter_version="1.0.0",
        confidence=Confidence.MEDIUM,
        calculation_artifact=artifact_ref,
    )


@pytest.fixture
def measured_provenance(artifact_ref: ArtifactRef) -> MeasuredProvenance:
    return MeasuredProvenance(
        provider="evalscope",
        provider_version="1.10.0",
        adapter_version="1.0.0",
        raw_artifact=artifact_ref,
    )


@pytest.fixture
def derived_provenance(artifact_ref: ArtifactRef) -> DerivedProvenance:
    return DerivedProvenance(
        provider="autopilot-champion-policy",
        provider_version="1.0.0",
        adapter_version="1.0.0",
        calculation_artifact=artifact_ref,
        input_artifacts=(artifact_ref,),
    )
