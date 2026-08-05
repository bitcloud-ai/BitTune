import hashlib

import pytest

from autopilot.domain.artifacts import ArtifactProducer, ArtifactRef
from autopilot.domain.identifiers import ArtifactId, ModelRevision, Sha256Digest
from autopilot.domain.provenance import DerivedProvenance, MeasuredProvenance
from autopilot.domain.workloads import (
    SamplingSpec,
    SyntheticFixedDataset,
    TokenizerRef,
    WorkloadSpec,
)


@pytest.fixture
def capability_artifact_ref() -> ArtifactRef:
    raw = b"capability-evidence"
    return ArtifactRef(
        artifact_id=ArtifactId(root=f"artifact_{'9' * 32}"),
        sha256=Sha256Digest(root=f"sha256:{hashlib.sha256(raw).hexdigest()}"),
        content_type="application/json",
        size_bytes=len(raw),
        producer=ArtifactProducer(component="unit-test", version="1.0.0"),
    )


@pytest.fixture
def capability_measured_provenance(
    capability_artifact_ref: ArtifactRef,
) -> MeasuredProvenance:
    return MeasuredProvenance(
        provider="evalscope",
        provider_version="test",
        adapter_version="test",
        raw_artifact=capability_artifact_ref,
    )


@pytest.fixture
def capability_derived_provenance(
    capability_artifact_ref: ArtifactRef,
) -> DerivedProvenance:
    return DerivedProvenance(
        provider="autopilot-champion-policy",
        provider_version="1.0.0",
        adapter_version="1.0.0",
        calculation_artifact=capability_artifact_ref,
        input_artifacts=(capability_artifact_ref,),
    )


@pytest.fixture
def capability_workload() -> WorkloadSpec:
    revision = ModelRevision(root="b" * 40)
    return WorkloadSpec(
        dataset=SyntheticFixedDataset(dataset_id="medium-v1"),
        tokenizer=TokenizerRef(repository_id="Qwen/Qwen3-8B", revision=revision),
        prompt_tokens=2_048,
        output_tokens=512,
        stream=True,
        ignore_eos=True,
        sampling=SamplingSpec(seed=20_260_805),
    )
