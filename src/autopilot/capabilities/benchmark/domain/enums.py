"""Stable benchmark capability enums."""

from enum import StrEnum


class SlaSearchVariable(StrEnum):
    RATE = "rate"
    CONCURRENCY = "concurrency"


class LatencyUnit(StrEnum):
    MILLISECONDS = "milliseconds"
    SECONDS = "seconds"


class BenchmarkValidationCode(StrEnum):
    VERSION_MISMATCH = "BENCHMARK_VERSION_MISMATCH"
    TRAFFIC_UNSUPPORTED = "BENCHMARK_TRAFFIC_UNSUPPORTED"
    METRIC_UNSUPPORTED = "BENCHMARK_METRIC_UNSUPPORTED"
    BUDGET_EXCEEDED = "BENCHMARK_BUDGET_EXCEEDED"
    INVALID_RAW_REPORT = "BENCHMARK_INVALID_RAW_REPORT"
    PROVIDER_UNAVAILABLE = "BENCHMARK_PROVIDER_UNAVAILABLE"
    PROFILE_UNVERIFIED = "BENCHMARK_PROFILE_UNVERIFIED"
    RUNNER_REJECTED = "BENCHMARK_RUNNER_REJECTED"
    ARTIFACT_BINDING = "BENCHMARK_ARTIFACT_BINDING"


class BenchmarkProviderState(StrEnum):
    """Normalized EvalScope lifecycle states."""

    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    CANCELLED = "cancelled"
    FAILED = "failed"
