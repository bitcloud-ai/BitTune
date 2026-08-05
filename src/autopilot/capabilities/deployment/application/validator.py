"""Pure validation for immutable deployment execution specifications."""

from typing import Never

from autopilot.capabilities.deployment.domain.enums import DeploymentValidationCode
from autopilot.capabilities.deployment.domain.errors import DeploymentValidationError
from autopilot.capabilities.deployment.domain.models import (
    DeploymentExecutionSpecification,
    VllmVersionProfile,
)
from autopilot.domain.candidates import (
    validate_candidate_workload,
    vllm_scheduler_parameters_are_compatible,
)
from autopilot.domain.hashing import compute_content_hash


def _reject(code: DeploymentValidationCode, field: str, message: str) -> Never:
    raise DeploymentValidationError(code, field, message)


def validate_deployment_specification(
    specification: DeploymentExecutionSpecification,
    profile: VllmVersionProfile,
) -> None:
    """Validate version, evidence, budget, and supported vLLM parameters."""
    candidate = specification.candidate
    parameters = candidate.parameters
    expected_versions = (
        specification.provider_version == profile.provider_version,
        specification.adapter_version == profile.adapter_version,
        specification.provider_profile_version == profile.profile_version,
        candidate.engine_version == profile.provider_version,
        candidate.adapter_version == profile.adapter_version,
    )
    if not all(expected_versions):
        _reject(
            DeploymentValidationCode.VERSION_MISMATCH,
            "provider_version",
            "deployment specification, candidate, and verified profile versions must match",
        )
    if candidate.engine_image != profile.engine_image:
        _reject(
            DeploymentValidationCode.IMAGE_MISMATCH,
            "candidate.engine_image",
            "candidate image does not match the verified vLLM profile digest",
        )
    if compute_content_hash(specification.workload) != candidate.workload_hash:
        _reject(
            DeploymentValidationCode.WORKLOAD_MISMATCH,
            "candidate.workload_hash",
            "candidate workload hash does not match the immutable deployment workload",
        )
    try:
        validate_candidate_workload(candidate, specification.workload)
    except ValueError as exc:
        raise DeploymentValidationError(
            DeploymentValidationCode.CONTEXT_EXCEEDED,
            "candidate.parameters.max_model_len",
            str(exc),
        ) from exc
    if (
        parameters.max_model_len > profile.max_model_len_upper_bound
        or not (
            profile.gpu_memory_utilization_min
            <= parameters.gpu_memory_utilization
            <= profile.gpu_memory_utilization_max
        )
        or parameters.max_num_seqs not in profile.supported_max_num_seqs
        or parameters.max_num_batched_tokens not in profile.supported_max_num_batched_tokens
        or (parameters.enable_chunked_prefill and not profile.supports_chunked_prefill)
        or not vllm_scheduler_parameters_are_compatible(
            max_model_len=parameters.max_model_len,
            max_num_batched_tokens=parameters.max_num_batched_tokens,
            enable_chunked_prefill=parameters.enable_chunked_prefill,
        )
    ):
        _reject(
            DeploymentValidationCode.PARAMETER_UNSUPPORTED,
            "candidate.parameters",
            "candidate contains a parameter not supported by the verified vLLM profile",
        )
    if not (
        profile.startup_timeout_seconds
        <= specification.budget.max_duration_seconds
        <= profile.max_task_timeout_seconds
    ):
        _reject(
            DeploymentValidationCode.BUDGET_EXCEEDED,
            "budget.max_duration_seconds",
            "deployment duration must cover startup without exceeding the profile task timeout",
        )
