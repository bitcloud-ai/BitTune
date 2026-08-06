"""Stable capacity planning enums."""

from enum import StrEnum


class CapacityValidationCode(StrEnum):
    """Classified capacity failures safe for application boundaries."""

    PROVIDER_UNAVAILABLE = "CAPACITY_PROVIDER_UNAVAILABLE"
    PROFILE_UNVERIFIED = "CAPACITY_PROFILE_UNVERIFIED"
    MODEL_UNSUPPORTED = "CAPACITY_MODEL_UNSUPPORTED"
    HARDWARE_MISMATCH = "CAPACITY_HARDWARE_MISMATCH"
    WORKLOAD_UNSUPPORTED = "CAPACITY_WORKLOAD_UNSUPPORTED"
    ESTIMATE_INVALID = "CAPACITY_ESTIMATE_INVALID"
    NO_FIT = "CAPACITY_NO_FIT"


class CandidateProfile(StrEnum):
    """The fixed candidate shapes exposed by the MVP."""

    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    THROUGHPUT = "throughput"
