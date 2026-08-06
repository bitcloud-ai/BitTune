import pytest

from autopilot.capabilities.capacity.adapters.fake import (
    FAKE_CAPACITY_PROFILE,
    FakeCapacityPlannerAdapter,
)
from autopilot.capabilities.capacity.adapters.llm_d_planner import LlmdPlannerAdapter
from autopilot.capabilities.capacity.domain.errors import CapacityProviderUnavailableError
from autopilot.capabilities.capacity.domain.models import CapacityPlanningSpecification
from autopilot.capabilities.environment.adapters.fake import (
    FAKE_ENVIRONMENT_PROFILE,
    FakeEnvironmentAdapter,
)
from autopilot.capabilities.environment.domain.models import EnvironmentInspectionSpecification
from autopilot.domain.identifiers import ModelRevision
from autopilot.domain.models import HuggingFaceModelRef
from autopilot.domain.workloads import (
    SamplingSpec,
    SyntheticFixedDataset,
    TokenizerRef,
    WorkloadSpec,
)


def _capacity_spec() -> CapacityPlanningSpecification:
    environment = FakeEnvironmentAdapter().inspect(
        EnvironmentInspectionSpecification(
            provider_version=FAKE_ENVIRONMENT_PROFILE.provider_version,
            adapter_version=FAKE_ENVIRONMENT_PROFILE.adapter_version,
            provider_profile_version=FAKE_ENVIRONMENT_PROFILE.profile_version,
        )
    )
    revision = ModelRevision(root="b" * 40)
    return CapacityPlanningSpecification(
        provider_version=FAKE_CAPACITY_PROFILE.provider_version,
        adapter_version=FAKE_CAPACITY_PROFILE.adapter_version,
        provider_profile_version=FAKE_CAPACITY_PROFILE.profile_version,
        model_ref=HuggingFaceModelRef(repository_id="Qwen/Qwen3-8B", revision=revision),
        hardware_passport=environment.hardware_passport,
        workload=WorkloadSpec(
            dataset=SyntheticFixedDataset(dataset_id="medium-v1"),
            tokenizer=TokenizerRef(repository_id="Qwen/Qwen3-8B", revision=revision),
            prompt_tokens=2_048,
            output_tokens=512,
            stream=True,
            ignore_eos=True,
            sampling=SamplingSpec(seed=20_260_805),
        ),
        requested_max_model_len=8_192,
        requested_gpu_memory_utilization=0.90,
        expected_concurrency=8,
    )


def test_fake_capacity_adapter_generates_three_ordered_candidates() -> None:
    plan = FakeCapacityPlannerAdapter().create_plan(_capacity_spec())

    assert tuple(candidate.profile for candidate in plan.candidates) == (
        "conservative",
        "balanced",
        "throughput",
    )
    assert plan.estimate.source == "estimated"
    assert plan.estimate.requires_benchmark_validation is True
    assert plan.plan_hash.root.startswith("sha256:")


def test_unconfigured_real_planner_fails_closed() -> None:
    adapter = LlmdPlannerAdapter(profile=FAKE_CAPACITY_PROFILE, client=None)

    with pytest.raises(CapacityProviderUnavailableError):
        adapter.validate(_capacity_spec())
