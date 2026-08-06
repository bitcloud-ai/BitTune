import pytest

from autopilot.capabilities.environment.adapters.fake import (
    FAKE_ENVIRONMENT_PROFILE,
    FakeEnvironmentAdapter,
)
from autopilot.capabilities.environment.domain.errors import EnvironmentValidationError
from autopilot.capabilities.environment.domain.models import EnvironmentInspectionSpecification


def _spec(**updates: str) -> EnvironmentInspectionSpecification:
    values = {
        "provider_version": FAKE_ENVIRONMENT_PROFILE.provider_version,
        "adapter_version": FAKE_ENVIRONMENT_PROFILE.adapter_version,
        "provider_profile_version": FAKE_ENVIRONMENT_PROFILE.profile_version,
    }
    values.update(updates)
    return EnvironmentInspectionSpecification(**values)


def test_fake_environment_adapter_returns_hardware_passport_with_stable_capabilities() -> None:
    result = FakeEnvironmentAdapter().inspect(_spec())

    assert result.hardware_passport_id == result.hardware_passport.hardware_passport_id
    assert result.hardware_passport.accelerators[0].name == "NVIDIA GeForce RTX 5090"
    assert result.hardware_passport.capabilities == (
        "single_nvidia_gpu",
        "docker_gpu",
        "vllm_single_gpu_candidate",
    )
    assert result.hardware_passport.provenance.source == "measured"


def test_environment_adapter_rejects_profile_drift() -> None:
    with pytest.raises(EnvironmentValidationError) as caught:
        FakeEnvironmentAdapter().inspect(_spec(provider_version="unverified"))

    assert caught.value.code.value == "ENVIRONMENT_PROFILE_UNVERIFIED"
