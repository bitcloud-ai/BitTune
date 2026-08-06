"""Stable environment capability enums."""

from enum import StrEnum


class EnvironmentValidationCode(StrEnum):
    """Classified failures at the environment boundary."""

    PROVIDER_UNAVAILABLE = "ENVIRONMENT_PROVIDER_UNAVAILABLE"
    PROFILE_UNVERIFIED = "ENVIRONMENT_PROFILE_UNVERIFIED"
    NON_LINUX_HOST = "ENVIRONMENT_NON_LINUX_HOST"
    GPU_COUNT_UNSUPPORTED = "ENVIRONMENT_GPU_COUNT_UNSUPPORTED"
    GPU_MODEL_UNSUPPORTED = "ENVIRONMENT_GPU_MODEL_UNSUPPORTED"
    GPU_BUSY = "ENVIRONMENT_GPU_BUSY"
    NVML_ERROR = "ENVIRONMENT_NVML_ERROR"
    INVALID_SNAPSHOT = "ENVIRONMENT_INVALID_SNAPSHOT"


class EnvironmentScope(StrEnum):
    """The only inspection scope supported by the MVP."""

    MVP_FULL = "mvp_full"
