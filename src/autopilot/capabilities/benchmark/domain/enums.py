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
