import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from autopilot.capabilities.benchmark.application.compiler import compile_benchmark
from autopilot.capabilities.benchmark.application.service import preview_benchmark
from autopilot.capabilities.benchmark.domain.enums import BenchmarkValidationCode
from autopilot.capabilities.benchmark.domain.errors import BenchmarkValidationError
from autopilot.capabilities.benchmark.domain.models import (
    BaselineTraffic,
    ClosedLoopSweepTraffic,
    CompiledEvalScopeBenchmark,
    ConcurrencySearchRange,
    EvalScopeVersionProfile,
    OpenLoopSweepTraffic,
    RateSearchRange,
    SlaSearchTraffic,
)
from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.enums import RiskLevel, TrafficMode

from .conftest import BenchmarkSpecificationFactory

GOLDEN_DIRECTORY = (
    Path(__file__).parents[4]
    / "src"
    / "autopilot"
    / "capabilities"
    / "benchmark"
    / "tests"
    / "golden"
)

TRAFFIC_CASES = {
    "baseline": BaselineTraffic(requests=5),
    "closed-loop": ClosedLoopSweepTraffic(
        concurrency_levels=(1, 2, 4),
        requests_per_worker=5,
    ),
    "open-loop": OpenLoopSweepTraffic(
        request_rates=(0.5, 1.0, 2.0),
        duration_seconds=60,
    ),
    "sla": SlaSearchTraffic(
        search=RateSearchRange(lower_bound=1, upper_bound=5, duration_seconds=30),
        runs_per_level=2,
        max_levels=3,
    ),
}


@pytest.mark.parametrize(("golden_name", "traffic"), TRAFFIC_CASES.items())
def test_compile_benchmark_matches_golden(
    golden_name: str,
    traffic,
    benchmark_specification_factory: BenchmarkSpecificationFactory,
    evalscope_profile: EvalScopeVersionProfile,
) -> None:
    specification = benchmark_specification_factory(traffic)
    compiled = compile_benchmark(specification, evalscope_profile)
    expected = json.loads(
        (GOLDEN_DIRECTORY / f"{golden_name}.expected.json").read_text(encoding="utf-8")
    )

    assert compiled.model_dump(mode="json") == expected


def test_closed_loop_number_is_compiler_owned(
    benchmark_specification_factory: BenchmarkSpecificationFactory,
    evalscope_profile: EvalScopeVersionProfile,
) -> None:
    specification = benchmark_specification_factory(TRAFFIC_CASES["closed-loop"])
    compiled = compile_benchmark(specification, evalscope_profile)

    assert compiled.traffic.mode is TrafficMode.CLOSED_LOOP_SWEEP
    assert compiled.traffic.number == (5, 10, 20)


def test_open_loop_has_no_parallel_surface_and_requires_approval(
    benchmark_specification_factory: BenchmarkSpecificationFactory,
    evalscope_profile: EvalScopeVersionProfile,
) -> None:
    specification = benchmark_specification_factory(TRAFFIC_CASES["open-loop"])
    preview = preview_benchmark(specification, evalscope_profile)
    payload = preview.compiled.traffic.model_dump(mode="json")

    assert "parallel" not in payload
    assert preview.execution_risk is RiskLevel.L2
    assert preview.requires_human_approval is True


def test_closed_loop_preview_is_l1_without_approval(
    benchmark_specification_factory: BenchmarkSpecificationFactory,
    evalscope_profile: EvalScopeVersionProfile,
) -> None:
    specification = benchmark_specification_factory(TRAFFIC_CASES["closed-loop"])
    preview = preview_benchmark(specification, evalscope_profile)

    assert preview.execution_risk is RiskLevel.L1
    assert preview.requires_human_approval is False


def test_sla_concurrency_uses_closed_loop_request_budget(
    benchmark_specification_factory: BenchmarkSpecificationFactory,
    evalscope_profile: EvalScopeVersionProfile,
) -> None:
    traffic = SlaSearchTraffic(
        search=ConcurrencySearchRange(
            lower_bound=1,
            upper_bound=8,
            requests_per_worker=5,
        ),
        runs_per_level=2,
        max_levels=3,
    )
    compiled = compile_benchmark(benchmark_specification_factory(traffic), evalscope_profile)

    assert compiled.traffic.search.number_per_run == 40
    assert compiled.budget_estimate.measurement_requests == 240
    assert compiled.budget_estimate.estimated_duration_seconds == 406


def test_sla_compiler_converts_latency_thresholds_and_budgets_warmup_per_run(
    benchmark_specification_factory: BenchmarkSpecificationFactory,
    evalscope_profile: EvalScopeVersionProfile,
) -> None:
    compiled = compile_benchmark(
        benchmark_specification_factory(TRAFFIC_CASES["sla"]),
        evalscope_profile,
    )

    assert compiled.traffic.constraints[0].value == 2.0
    assert compiled.budget_estimate.warmup_requests == 102
    assert compiled.budget_estimate.total_requests == 1_092


def test_compiler_recalculates_and_rejects_budget(
    benchmark_specification_factory: BenchmarkSpecificationFactory,
    evalscope_profile: EvalScopeVersionProfile,
) -> None:
    specification = benchmark_specification_factory(TRAFFIC_CASES["open-loop"])
    constrained = specification.model_copy(
        update={
            "budget": ExecutionBudget(
                max_duration_seconds=1_800,
                max_requests=100,
                max_input_tokens=1_000_000,
                max_output_tokens=1_000_000,
                max_disk_growth_bytes=2_000_000_000,
            )
        }
    )

    with pytest.raises(BenchmarkValidationError) as caught:
        compile_benchmark(constrained, evalscope_profile)

    assert caught.value.code is BenchmarkValidationCode.BUDGET_EXCEEDED


def test_compiled_benchmark_has_no_endpoint_or_provider_escape_hatch(
    benchmark_specification_factory: BenchmarkSpecificationFactory,
    evalscope_profile: EvalScopeVersionProfile,
) -> None:
    specification = benchmark_specification_factory(TRAFFIC_CASES["baseline"])
    serialized = json.dumps(
        compile_benchmark(specification, evalscope_profile).model_dump(mode="json")
    )

    assert all(
        forbidden not in serialized
        for forbidden in ("url", "header", "secret", "extra_args", "command", "output_path")
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("measurement_requests", 232),
        ("warmup_requests", 24),
        ("estimated_input_tokens", 25_601),
        ("estimated_duration_seconds", 1_801),
    ],
)
def test_compiled_benchmark_rejects_tampered_budget_estimate(
    field: str,
    value: int,
    benchmark_specification_factory: BenchmarkSpecificationFactory,
    evalscope_profile: EvalScopeVersionProfile,
) -> None:
    compiled = compile_benchmark(
        benchmark_specification_factory(TRAFFIC_CASES["open-loop"]),
        evalscope_profile,
    )
    payload = compiled.model_dump(mode="json")
    payload["budget_estimate"][field] = value

    with pytest.raises(ValidationError, match="budget estimate"):
        CompiledEvalScopeBenchmark.model_validate(payload)
