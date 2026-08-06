"""Pure capacity profile and input validation."""

from autopilot.capabilities.capacity.domain.enums import CapacityValidationCode
from autopilot.capabilities.capacity.domain.errors import CapacityValidationError
from autopilot.capabilities.capacity.domain.models import (
    CapacityPlannerVersionProfile,
    CapacityPlanningSpecification,
)
from autopilot.domain.hashing import compute_content_hash


def validate_capacity_specification(
    specification: CapacityPlanningSpecification,
    profile: CapacityPlannerVersionProfile,
) -> None:
    """Require an exact verified Planner profile and a usable Hardware Passport."""
    if specification.provider_version != profile.provider_version:
        raise CapacityValidationError(
            CapacityValidationCode.PROFILE_UNVERIFIED,
            "capacity provider version does not match the verified Planner profile",
            "provider_version",
        )
    if specification.adapter_version != profile.adapter_version:
        raise CapacityValidationError(
            CapacityValidationCode.PROFILE_UNVERIFIED,
            "capacity adapter version does not match the verified Planner profile",
            "adapter_version",
        )
    if specification.provider_profile_version != profile.profile_version:
        raise CapacityValidationError(
            CapacityValidationCode.PROFILE_UNVERIFIED,
            "capacity profile version does not match the verified Planner profile",
            "provider_profile_version",
        )
    if len(specification.hardware_passport.accelerators) != 1 or not {
        "single_nvidia_gpu",
        "docker_gpu",
        "vllm_single_gpu_candidate",
    }.issubset(specification.hardware_passport.capabilities):
        raise CapacityValidationError(
            CapacityValidationCode.HARDWARE_MISMATCH,
            "Hardware Passport does not prove the required single-GPU capabilities",
            "hardware_passport.capabilities",
        )


def hardware_passport_hash(specification: CapacityPlanningSpecification) -> str:
    """Return the canonical Hardware Passport hash used by Candidate binding."""
    return compute_content_hash(specification.hardware_passport).root
