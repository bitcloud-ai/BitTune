import pytest

from autopilot.capabilities.optimization.application.search_space import (
    enumerate_search_space,
    validate_trial_parameters,
)
from autopilot.capabilities.optimization.domain.enums import OptimizationValidationCode
from autopilot.capabilities.optimization.domain.errors import OptimizationValidationError
from autopilot.capabilities.optimization.domain.models import (
    GpuMemoryUtilizationRange,
    VllmSearchSpaceSpec,
)
from autopilot.domain.candidates import VllmTuningSpec
from autopilot.domain.constraints import BooleanConstraint, ObjectiveSpec, SloSpec
from autopilot.domain.workloads import WorkloadSpec


def base_parameters(max_model_len: int = 8_192) -> VllmTuningSpec:
    return VllmTuningSpec(
        max_model_len=max_model_len,
        gpu_memory_utilization=0.8,
        max_num_seqs=4,
        max_num_batched_tokens=2_048,
        enable_chunked_prefill=True,
    )


def search_space() -> VllmSearchSpaceSpec:
    return VllmSearchSpaceSpec(
        profile_name="balanced-v1",
        objective=ObjectiveSpec(),
        slo=SloSpec(constraints=(BooleanConstraint(),)),
        gpu_memory_utilization=GpuMemoryUtilizationRange(low=0.8, high=0.84, step=0.02),
        max_num_seqs=(4, 8),
        max_num_batched_tokens=(2_048,),
        enable_chunked_prefill=(False, True),
    )


def test_search_space_enumeration_is_complete_and_deterministic() -> None:
    combinations = enumerate_search_space(base_parameters(), search_space())

    assert len(combinations) == 6
    assert combinations[0].gpu_memory_utilization == 0.8
    assert combinations[-1].gpu_memory_utilization == 0.84
    assert combinations[-1].enable_chunked_prefill is True


def test_search_space_rejects_when_static_pruning_removes_every_combination() -> None:
    only_unschedulable = search_space().model_copy(update={"enable_chunked_prefill": (False,)})

    with pytest.raises(OptimizationValidationError) as caught:
        enumerate_search_space(base_parameters(), only_unschedulable)

    assert caught.value.code is OptimizationValidationCode.STATIC_REJECTED


def test_trial_validation_rejects_changed_fixed_or_search_parameters(
    capability_workload: WorkloadSpec,
) -> None:
    base = base_parameters()
    outside = base.model_copy(update={"max_num_seqs": 32})

    with pytest.raises(OptimizationValidationError) as caught:
        validate_trial_parameters(outside, base, search_space(), capability_workload)

    assert caught.value.code is OptimizationValidationCode.OUTSIDE_SEARCH_SPACE

    changed_context = base.model_copy(update={"max_model_len": 16_384})
    with pytest.raises(OptimizationValidationError) as changed:
        validate_trial_parameters(changed_context, base, search_space(), capability_workload)

    assert changed.value.code is OptimizationValidationCode.OUTSIDE_SEARCH_SPACE


def test_static_pruning_rejects_workload_context(capability_workload: WorkloadSpec) -> None:
    parameters = base_parameters(max_model_len=2_500)

    with pytest.raises(OptimizationValidationError) as caught:
        validate_trial_parameters(parameters, parameters, search_space(), capability_workload)

    assert caught.value.code is OptimizationValidationCode.STATIC_REJECTED


def test_static_pruning_rejects_unschedulable_full_prefill(
    capability_workload: WorkloadSpec,
) -> None:
    base = base_parameters()
    parameters = base.model_copy(update={"enable_chunked_prefill": False})

    with pytest.raises(OptimizationValidationError) as caught:
        validate_trial_parameters(parameters, base, search_space(), capability_workload)

    assert caught.value.code is OptimizationValidationCode.STATIC_REJECTED


def test_search_space_rejects_generic_escape_hatch() -> None:
    with pytest.raises(ValueError, match="extra_args"):
        VllmSearchSpaceSpec.model_validate({**search_space().model_dump(), "extra_args": {}})


@pytest.mark.parametrize(
    "range_values",
    [
        {"low": 0.8, "high": 0.8, "step": 0.01},
        {"low": 0.8, "high": 0.94, "step": 0.001},
    ],
)
def test_gpu_memory_grid_rejects_degenerate_or_unbounded_ranges(
    range_values: dict[str, float],
) -> None:
    with pytest.raises(ValueError, match="at most 15 values"):
        GpuMemoryUtilizationRange(**range_values)


def test_search_space_requires_canonical_boolean_choice_order() -> None:
    with pytest.raises(ValueError, match="unique and ordered"):
        VllmSearchSpaceSpec(
            **search_space().model_dump(exclude={"enable_chunked_prefill"}),
            enable_chunked_prefill=(True, False),
        )
