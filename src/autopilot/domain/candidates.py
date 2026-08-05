"""Single-GPU vLLM candidate contract."""

from typing import Literal

from pydantic import Field

from autopilot.domain.base import NonEmptyStr, StrictModel
from autopilot.domain.identifiers import (
    CandidateId,
    HardwarePassportId,
    ImageDigest,
    ModelProfileId,
    Sha256Digest,
)
from autopilot.domain.models import ModelRef
from autopilot.domain.provenance import EstimatedProvenance
from autopilot.domain.workloads import WorkloadSpec

CANDIDATE_CONTEXT_EXCEEDED = "prompt and output tokens exceed candidate max_model_len"


class VllmTuningSpec(StrictModel):
    tensor_parallel_size: Literal[1] = 1
    max_model_len: int = Field(ge=1, le=50_000_000)
    gpu_memory_utilization: float = Field(ge=0.80, le=0.94)
    max_num_seqs: Literal[4, 8, 16, 32]
    max_num_batched_tokens: Literal[2048, 4096, 8192, 16384]
    enable_chunked_prefill: bool
    trust_remote_code: Literal[False] = False


class DeploymentCandidate(StrictModel):
    schema_version: Literal["deployment-candidate/v1"] = "deployment-candidate/v1"
    candidate_id: CandidateId
    profile: Literal["conservative", "balanced", "throughput"]
    hardware_passport_id: HardwarePassportId
    hardware_passport_hash: Sha256Digest
    model_profile_id: ModelProfileId
    model_ref: ModelRef
    engine: Literal["vllm"] = "vllm"
    engine_image: ImageDigest
    engine_version: NonEmptyStr
    adapter_version: NonEmptyStr
    workload_hash: Sha256Digest
    parameters: VllmTuningSpec
    estimation: EstimatedProvenance


def validate_candidate_workload(candidate: DeploymentCandidate, workload: WorkloadSpec) -> None:
    """Reject a candidate whose model context is shorter than the fixed workload."""
    if workload.prompt_tokens + workload.output_tokens > candidate.parameters.max_model_len:
        raise ValueError(CANDIDATE_CONTEXT_EXCEEDED)
