"""Single-GPU vLLM candidate contract."""

from typing import Literal, Self

from pydantic import Field, model_validator

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
INVALID_VLLM_SCHEDULER_BATCH = (
    "max_num_batched_tokens must cover max_model_len when chunked prefill is disabled"
)


def vllm_scheduler_parameters_are_compatible(
    *,
    max_model_len: int,
    max_num_batched_tokens: int,
    enable_chunked_prefill: bool,
) -> bool:
    """Return whether vLLM can schedule a full prefill under the fixed MVP rule."""
    return enable_chunked_prefill or max_num_batched_tokens >= max_model_len


class VllmTuningSpec(StrictModel):
    tensor_parallel_size: Literal[1] = 1
    max_model_len: int = Field(ge=1, le=50_000_000)
    gpu_memory_utilization: float = Field(ge=0.80, le=0.94)
    max_num_seqs: Literal[4, 8, 16, 32]
    max_num_batched_tokens: Literal[2048, 4096, 8192, 16384]
    enable_chunked_prefill: bool
    trust_remote_code: Literal[False] = False

    @model_validator(mode="after")
    def validate_scheduler_batch(self) -> Self:
        if not vllm_scheduler_parameters_are_compatible(
            max_model_len=self.max_model_len,
            max_num_batched_tokens=self.max_num_batched_tokens,
            enable_chunked_prefill=self.enable_chunked_prefill,
        ):
            raise ValueError(INVALID_VLLM_SCHEDULER_BATCH)
        return self


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
