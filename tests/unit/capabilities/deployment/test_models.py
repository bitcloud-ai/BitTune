import pytest
from pydantic import ValidationError

from autopilot.capabilities.deployment.domain.models import (
    DeploymentHealthPolicy,
    DeploymentRuntimeLimits,
    VllmVersionProfile,
)


def test_vllm_profile_requires_rtx_5090_verification(
    vllm_profile: VllmVersionProfile,
) -> None:
    with pytest.raises(ValidationError, match="rtx_5090_verified"):
        VllmVersionProfile.model_validate({**vllm_profile.model_dump(), "rtx_5090_verified": False})


def test_vllm_profile_rejects_unknown_provider_options(
    vllm_profile: VllmVersionProfile,
) -> None:
    with pytest.raises(ValidationError, match="extra_args"):
        VllmVersionProfile.model_validate({**vllm_profile.model_dump(), "extra_args": {}})


def test_vllm_profile_requires_canonical_supported_value_order(
    vllm_profile: VllmVersionProfile,
) -> None:
    with pytest.raises(ValidationError, match="unique and ordered"):
        VllmVersionProfile.model_validate(
            {
                **vllm_profile.model_dump(),
                "supported_max_num_seqs": [8, 4],
            }
        )


def test_deployment_runtime_and_health_policy_cannot_weaken_mandatory_limits() -> None:
    with pytest.raises(ValidationError, match="task timeout"):
        DeploymentRuntimeLimits(
            pid_limit=1_024,
            startup_timeout_seconds=600,
            task_timeout_seconds=300,
            health_check_timeout_seconds=30,
            max_disk_growth_bytes=1_000_000,
        )

    with pytest.raises(ValidationError, match="mandatory check"):
        DeploymentHealthPolicy(required_checks=())
