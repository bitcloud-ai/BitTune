"""Enumerate and statically validate the closed MVP vLLM search space."""

from decimal import Decimal
from itertools import product

from autopilot.capabilities.optimization.domain.enums import OptimizationValidationCode
from autopilot.capabilities.optimization.domain.errors import OptimizationValidationError
from autopilot.capabilities.optimization.domain.models import VllmSearchSpaceSpec
from autopilot.domain.candidates import (
    VllmTuningSpec,
    vllm_scheduler_parameters_are_compatible,
)
from autopilot.domain.workloads import WorkloadSpec


def gpu_memory_values(search_space: VllmSearchSpaceSpec) -> tuple[float, ...]:
    """Return the exact decimal grid encoded by the versioned search profile."""
    value_range = search_space.gpu_memory_utilization
    low = Decimal(str(value_range.low))
    high = Decimal(str(value_range.high))
    step = Decimal(str(value_range.step))
    count = int((high - low) / step) + 1
    return tuple(float(low + step * index) for index in range(count))


def enumerate_search_space(
    base_parameters: VllmTuningSpec,
    search_space: VllmSearchSpaceSpec,
) -> tuple[VllmTuningSpec, ...]:
    """Enumerate every allowed combination without calling Optuna."""
    combinations = product(
        gpu_memory_values(search_space),
        search_space.max_num_seqs,
        search_space.max_num_batched_tokens,
        search_space.enable_chunked_prefill,
    )
    candidates = tuple(
        VllmTuningSpec(
            max_model_len=base_parameters.max_model_len,
            gpu_memory_utilization=gpu_memory_utilization,
            max_num_seqs=max_num_seqs,
            max_num_batched_tokens=max_num_batched_tokens,
            enable_chunked_prefill=enable_chunked_prefill,
        )
        for (
            gpu_memory_utilization,
            max_num_seqs,
            max_num_batched_tokens,
            enable_chunked_prefill,
        ) in combinations
        if vllm_scheduler_parameters_are_compatible(
            max_model_len=base_parameters.max_model_len,
            max_num_batched_tokens=max_num_batched_tokens,
            enable_chunked_prefill=enable_chunked_prefill,
        )
    )
    if not candidates:
        raise OptimizationValidationError(
            OptimizationValidationCode.STATIC_REJECTED,
            "search_space",
            "search space has no scheduler-compatible parameter combination",
        )
    return candidates


def validate_trial_parameters(
    parameters: VllmTuningSpec,
    base_parameters: VllmTuningSpec,
    search_space: VllmSearchSpaceSpec,
    workload: WorkloadSpec,
) -> None:
    """Reject a proposal outside the search profile or fixed workload context."""
    if (
        parameters.max_model_len != base_parameters.max_model_len
        or parameters.gpu_memory_utilization not in gpu_memory_values(search_space)
        or parameters.max_num_seqs not in search_space.max_num_seqs
        or parameters.max_num_batched_tokens not in search_space.max_num_batched_tokens
        or parameters.enable_chunked_prefill not in search_space.enable_chunked_prefill
    ):
        raise OptimizationValidationError(
            OptimizationValidationCode.OUTSIDE_SEARCH_SPACE,
            "parameters",
            "trial parameters are outside the immutable search-space profile",
        )
    if workload.prompt_tokens + workload.output_tokens > parameters.max_model_len:
        raise OptimizationValidationError(
            OptimizationValidationCode.STATIC_REJECTED,
            "parameters.max_model_len",
            "trial context is shorter than the fixed workload",
        )
    if not vllm_scheduler_parameters_are_compatible(
        max_model_len=parameters.max_model_len,
        max_num_batched_tokens=parameters.max_num_batched_tokens,
        enable_chunked_prefill=parameters.enable_chunked_prefill,
    ):
        raise OptimizationValidationError(
            OptimizationValidationCode.STATIC_REJECTED,
            "parameters.max_num_batched_tokens",
            "trial cannot schedule a full prefill with chunked prefill disabled",
        )
