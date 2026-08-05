import json
from pathlib import Path

import pytest

from autopilot.capabilities.benchmark.application.normalizer import normalize_evalscope_report
from autopilot.capabilities.benchmark.domain.enums import BenchmarkValidationCode, LatencyUnit
from autopilot.capabilities.benchmark.domain.errors import BenchmarkValidationError
from autopilot.capabilities.benchmark.domain.models import (
    CompiledEvalScopeBenchmark,
    EvalScopeVersionProfile,
)
from autopilot.capabilities.benchmark.ports.models import EvalScopeRawReport
from autopilot.domain.enums import TrafficMode

GOLDEN_PATH = (
    Path(__file__).parents[4]
    / "src"
    / "autopilot"
    / "capabilities"
    / "benchmark"
    / "tests"
    / "golden"
    / "normalized-result.expected.json"
)


def test_normalizer_matches_metric_golden(
    evalscope_raw_report: EvalScopeRawReport,
    compiled_open_loop_benchmark: CompiledEvalScopeBenchmark,
    evalscope_profile: EvalScopeVersionProfile,
) -> None:
    result = normalize_evalscope_report(
        evalscope_raw_report,
        compiled_open_loop_benchmark,
        evalscope_profile,
    )
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

    assert result.model_dump(mode="json") == expected


def test_normalizer_rejects_missing_required_metric(
    evalscope_raw_report: EvalScopeRawReport,
    compiled_open_loop_benchmark: CompiledEvalScopeBenchmark,
    evalscope_profile: EvalScopeVersionProfile,
) -> None:
    incomplete = evalscope_raw_report.model_copy(
        update={"metrics": evalscope_raw_report.metrics[1:]}
    )

    with pytest.raises(BenchmarkValidationError) as caught:
        normalize_evalscope_report(incomplete, compiled_open_loop_benchmark, evalscope_profile)

    assert caught.value.code is BenchmarkValidationCode.INVALID_RAW_REPORT


def test_normalizer_rejects_inconsistent_counts(
    evalscope_raw_report: EvalScopeRawReport,
    compiled_open_loop_benchmark: CompiledEvalScopeBenchmark,
    evalscope_profile: EvalScopeVersionProfile,
) -> None:
    samples = tuple(
        sample.model_copy(update={"value": 9}) if sample.name == "completed" else sample
        for sample in evalscope_raw_report.metrics
    )
    inconsistent = evalscope_raw_report.model_copy(update={"metrics": samples})

    with pytest.raises(BenchmarkValidationError, match="inconsistent"):
        normalize_evalscope_report(inconsistent, compiled_open_loop_benchmark, evalscope_profile)


def test_normalizer_rejects_reinterpreted_profile_or_traffic_mode(
    evalscope_raw_report: EvalScopeRawReport,
    compiled_open_loop_benchmark: CompiledEvalScopeBenchmark,
    evalscope_profile: EvalScopeVersionProfile,
) -> None:
    changed_profile = evalscope_profile.model_copy(
        update={"latency_unit": LatencyUnit.MILLISECONDS}
    )
    with pytest.raises(BenchmarkValidationError) as changed:
        normalize_evalscope_report(
            evalscope_raw_report,
            compiled_open_loop_benchmark,
            changed_profile,
        )
    assert changed.value.code is BenchmarkValidationCode.INVALID_RAW_REPORT

    changed_mode = evalscope_raw_report.model_copy(update={"traffic_mode": TrafficMode.BASELINE})
    with pytest.raises(BenchmarkValidationError) as changed:
        normalize_evalscope_report(
            changed_mode,
            compiled_open_loop_benchmark,
            evalscope_profile,
        )
    assert changed.value.code is BenchmarkValidationCode.INVALID_RAW_REPORT


def test_normalizer_maps_derived_overflow_to_typed_error(
    evalscope_raw_report: EvalScopeRawReport,
    compiled_open_loop_benchmark: CompiledEvalScopeBenchmark,
    evalscope_profile: EvalScopeVersionProfile,
) -> None:
    samples = tuple(
        sample.model_copy(update={"value": 1e308}) if sample.name == "e2e_seconds_p99" else sample
        for sample in evalscope_raw_report.metrics
    )
    overflowing = evalscope_raw_report.model_copy(update={"metrics": samples})

    with pytest.raises(BenchmarkValidationError) as caught:
        normalize_evalscope_report(
            overflowing,
            compiled_open_loop_benchmark,
            evalscope_profile,
        )

    assert caught.value.code is BenchmarkValidationCode.INVALID_RAW_REPORT


def test_normalizer_rejects_measurement_beyond_approved_deadline(
    evalscope_raw_report: EvalScopeRawReport,
    compiled_open_loop_benchmark: CompiledEvalScopeBenchmark,
    evalscope_profile: EvalScopeVersionProfile,
) -> None:
    samples = tuple(
        sample.model_copy(update={"value": 1_801})
        if sample.name == "measurement_duration_seconds"
        else sample
        for sample in evalscope_raw_report.metrics
    )
    over_deadline = evalscope_raw_report.model_copy(update={"metrics": samples})

    with pytest.raises(BenchmarkValidationError) as caught:
        normalize_evalscope_report(
            over_deadline,
            compiled_open_loop_benchmark,
            evalscope_profile,
        )

    assert caught.value.code is BenchmarkValidationCode.BUDGET_EXCEEDED


def test_normalizer_rejects_output_length_beyond_compiled_limit(
    evalscope_raw_report: EvalScopeRawReport,
    compiled_open_loop_benchmark: CompiledEvalScopeBenchmark,
    evalscope_profile: EvalScopeVersionProfile,
) -> None:
    samples = tuple(
        sample.model_copy(update={"value": compiled_open_loop_benchmark.workload.output_tokens + 1})
        if sample.name == "output_tokens_p99"
        else sample
        for sample in evalscope_raw_report.metrics
    )
    invalid_lengths = evalscope_raw_report.model_copy(update={"metrics": samples})

    with pytest.raises(BenchmarkValidationError) as caught:
        normalize_evalscope_report(
            invalid_lengths,
            compiled_open_loop_benchmark,
            evalscope_profile,
        )

    assert caught.value.code is BenchmarkValidationCode.INVALID_RAW_REPORT
