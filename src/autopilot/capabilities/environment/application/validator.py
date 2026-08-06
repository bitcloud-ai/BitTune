"""Pure environment profile and compatibility validation."""

from autopilot.capabilities.environment.domain.enums import EnvironmentValidationCode
from autopilot.capabilities.environment.domain.errors import EnvironmentValidationError
from autopilot.capabilities.environment.domain.models import (
    EnvironmentInspectionSpecification,
    EnvironmentVersionProfile,
    HostSnapshot,
)


def validate_environment_specification(
    specification: EnvironmentInspectionSpecification,
    profile: EnvironmentVersionProfile,
) -> None:
    """Bind an inspection to one exact, verified provider profile."""
    if specification.provider_version != profile.provider_version:
        raise EnvironmentValidationError(
            EnvironmentValidationCode.PROFILE_UNVERIFIED,
            "environment provider version does not match the verified profile",
            "provider_version",
        )
    if specification.adapter_version != profile.adapter_version:
        raise EnvironmentValidationError(
            EnvironmentValidationCode.PROFILE_UNVERIFIED,
            "environment adapter version does not match the verified profile",
            "adapter_version",
        )
    if specification.provider_profile_version != profile.profile_version:
        raise EnvironmentValidationError(
            EnvironmentValidationCode.PROFILE_UNVERIFIED,
            "environment profile version does not match the verified profile",
            "provider_profile_version",
        )


def validate_rtx_5090_snapshot(snapshot: HostSnapshot, profile: EnvironmentVersionProfile) -> None:
    """Enforce the native Linux, single RTX 5090 Phase-0 contract."""
    if snapshot.os.name.lower() in {"windows", "darwin", "macos"}:
        raise EnvironmentValidationError(
            EnvironmentValidationCode.NON_LINUX_HOST,
            "the MVP environment adapter only supports native Linux hosts",
            "os.name",
        )
    if snapshot.gpu.name != profile.expected_gpu_name:
        raise EnvironmentValidationError(
            EnvironmentValidationCode.GPU_MODEL_UNSUPPORTED,
            "GPU 0 does not match the verified RTX 5090 profile",
            "gpu.name",
        )
    memory_delta = abs(snapshot.gpu.memory_total_bytes - profile.expected_memory_total_bytes)
    if memory_delta > profile.memory_tolerance_bytes:
        raise EnvironmentValidationError(
            EnvironmentValidationCode.GPU_MODEL_UNSUPPORTED,
            "GPU 0 memory does not match the verified RTX 5090 profile",
            "gpu.memory_total_bytes",
        )
