"""Normalize a pinned EvalScope raw report into provider-independent metrics."""

import math
from collections.abc import Callable

from pydantic import ValidationError

from autopilot.capabilities.benchmark.domain.enums import BenchmarkValidationCode, LatencyUnit
from autopilot.capabilities.benchmark.domain.errors import BenchmarkValidationError
from autopilot.capabilities.benchmark.domain.models import (
    BenchmarkResult,
    CompiledEvalScopeBenchmark,
    EvalScopeVersionProfile,
    LatencySummary,
    LengthSummary,
    PercentileFieldBindings,
    PercentileValues,
    ReliabilitySummary,
    ThroughputSummary,
)
from autopilot.capabilities.benchmark.ports.models import EvalScopeRawReport
from autopilot.domain.hashing import compute_content_hash
from autopilot.domain.provenance import MeasuredProvenance

MetricLookup = Callable[[str], float]


def _normalization_error(field: str, message: str) -> BenchmarkValidationError:
    return BenchmarkValidationError(
        BenchmarkValidationCode.INVALID_RAW_REPORT,
        field,
        message,
    )


def _count(value: float, field: str) -> int:
    if not math.isfinite(value) or value < 0 or not value.is_integer():
        raise _normalization_error(field, "raw count must be a non-negative integer")
    return int(value)


def _non_negative(value: float, field: str) -> float:
    if not math.isfinite(value) or value < 0:
        raise _normalization_error(field, "raw metric must be finite and non-negative")
    return value


def _percentiles(
    bindings: PercentileFieldBindings,
    lookup: MetricLookup,
    multiplier: float,
) -> PercentileValues:
    return PercentileValues(
        p50=_non_negative(lookup(bindings.p50) * multiplier, bindings.p50),
        p95=_non_negative(lookup(bindings.p95) * multiplier, bindings.p95),
        p99=_non_negative(lookup(bindings.p99) * multiplier, bindings.p99),
    )


def normalize_evalscope_report(
    report: EvalScopeRawReport,
    compiled: CompiledEvalScopeBenchmark,
    profile: EvalScopeVersionProfile,
) -> BenchmarkResult:
    """Normalize fixed raw fields and derive reliability and throughput metrics."""
    versions_match = (
        report.provider_version == compiled.provider_version == profile.provider_version
        and report.adapter_version == compiled.adapter_version == profile.adapter_version
        and report.provider_profile_version
        == compiled.provider_profile_version
        == profile.profile_version
    )
    if not versions_match:
        raise BenchmarkValidationError(
            BenchmarkValidationCode.VERSION_MISMATCH,
            "provider_version",
            "raw report, compiled benchmark, and verified profile versions must match",
        )
    if (
        report.provider_profile_hash != compute_content_hash(profile)
        or report.compiled_benchmark_hash != compute_content_hash(compiled)
        or report.traffic_mode is not compiled.traffic.mode
    ):
        raise _normalization_error(
            "execution_context",
            "raw report does not match the compiled benchmark and verified profile",
        )
    values = {sample.name: sample.value for sample in report.metrics}

    def lookup(name: str) -> float:
        try:
            return values[name]
        except KeyError as exc:
            raise _normalization_error(name, "required raw metric is missing") from exc

    bindings = profile.raw_metric_bindings
    reliability_fields = bindings.reliability
    submitted = _count(lookup(reliability_fields.submitted), reliability_fields.submitted)
    completed = _count(lookup(reliability_fields.completed), reliability_fields.completed)
    failed = _count(lookup(reliability_fields.failed), reliability_fields.failed)
    timed_out = _count(lookup(reliability_fields.timed_out), reliability_fields.timed_out)
    completed_within_window = _count(
        lookup(reliability_fields.completed_within_window),
        reliability_fields.completed_within_window,
    )
    scheduled_window_seconds = lookup(reliability_fields.scheduled_window_seconds)
    measurement_duration_seconds = lookup(reliability_fields.measurement_duration_seconds)
    if (
        submitted <= 0
        or completed + failed + timed_out != submitted
        or completed_within_window > completed
        or scheduled_window_seconds <= 0
        or measurement_duration_seconds < scheduled_window_seconds
    ):
        raise _normalization_error(
            "reliability",
            "request counts or benchmark window durations are inconsistent",
        )
    input_tokens = _count(
        lookup(bindings.tokens.successful_input_tokens),
        bindings.tokens.successful_input_tokens,
    )
    output_tokens = _count(
        lookup(bindings.tokens.successful_output_tokens),
        bindings.tokens.successful_output_tokens,
    )
    if (
        measurement_duration_seconds > compiled.execution_budget.max_duration_seconds
        or submitted > compiled.budget_estimate.measurement_requests
        or submitted > compiled.execution_budget.max_requests
        or input_tokens > compiled.execution_budget.max_input_tokens
        or output_tokens > compiled.execution_budget.max_output_tokens
        or input_tokens > completed * compiled.workload.prompt_tokens
        or output_tokens > completed * compiled.workload.output_tokens
    ):
        raise BenchmarkValidationError(
            BenchmarkValidationCode.BUDGET_EXCEEDED,
            "raw_report",
            "raw report exceeds the compiled request or token budget",
        )
    latency_multiplier = 1_000.0 if profile.latency_unit is LatencyUnit.SECONDS else 1.0
    try:
        latency = LatencySummary(
            e2e_ms=_percentiles(bindings.latency.e2e, lookup, latency_multiplier),
            ttft_ms=_percentiles(bindings.latency.ttft, lookup, latency_multiplier),
            tpot_ms=_percentiles(bindings.latency.tpot, lookup, latency_multiplier),
            itl_ms=_percentiles(bindings.latency.itl, lookup, latency_multiplier),
        )
        lengths = LengthSummary(
            input_tokens=_percentiles(bindings.lengths.input_tokens, lookup, 1),
            output_tokens=_percentiles(bindings.lengths.output_tokens, lookup, 1),
        )
        if lengths.output_tokens.p99 > compiled.workload.output_tokens:
            raise _normalization_error(
                bindings.lengths.output_tokens.p99,
                "output token percentile exceeds the compiled per-request limit",
            )
        throughput = ThroughputSummary(
            requests_per_second=completed / measurement_duration_seconds,
            successful_requests_per_minute=completed * 60 / measurement_duration_seconds,
            input_tokens_per_second=input_tokens / measurement_duration_seconds,
            successful_output_tokens_per_second=output_tokens / measurement_duration_seconds,
            total_tokens_per_minute=(input_tokens + output_tokens)
            * 60
            / measurement_duration_seconds,
        )
        reliability = ReliabilitySummary(
            submitted=submitted,
            completed=completed,
            failed=failed,
            timed_out=timed_out,
            completed_within_window=completed_within_window,
            success_rate=completed / submitted,
            window_completion_ratio=completed_within_window / submitted,
        )
        return BenchmarkResult(
            provider_version=profile.provider_version,
            adapter_version=profile.adapter_version,
            provider_profile_version=profile.profile_version,
            provider_profile_hash=report.provider_profile_hash,
            compiled_benchmark_hash=report.compiled_benchmark_hash,
            deployment_id=compiled.deployment_id,
            deployment_plan_hash=compiled.deployment_plan_hash,
            traffic_mode=compiled.traffic.mode,
            scheduled_window_seconds=scheduled_window_seconds,
            measurement_duration_seconds=measurement_duration_seconds,
            latency=latency,
            throughput=throughput,
            reliability=reliability,
            lengths=lengths,
            oom=report.oom,
            raw_report_hash=report.raw_artifact.sha256,
            provenance=MeasuredProvenance(
                provider="evalscope",
                provider_version=profile.provider_version,
                adapter_version=profile.adapter_version,
                raw_artifact=report.raw_artifact,
            ),
        )
    except ValidationError as error:
        raise _normalization_error(
            "metrics",
            "normalized metrics violate the benchmark result contract",
        ) from error
