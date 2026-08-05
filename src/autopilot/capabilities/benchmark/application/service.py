"""Read-only benchmark preview service."""

from typing import Literal

from autopilot.capabilities.benchmark.application.compiler import compile_benchmark
from autopilot.capabilities.benchmark.domain.models import (
    BenchmarkExecutionSpecification,
    CompiledEvalScopeBenchmark,
    EvalScopeVersionProfile,
    OpenLoopSweepTraffic,
    RateSearchRange,
    SlaSearchTraffic,
)
from autopilot.domain.base import StrictModel
from autopilot.domain.enums import RiskLevel


class BenchmarkPreview(StrictModel):
    schema_version: Literal["benchmark-preview/v1"] = "benchmark-preview/v1"
    compiled: CompiledEvalScopeBenchmark
    execution_risk: RiskLevel
    requires_human_approval: bool


def preview_benchmark(
    specification: BenchmarkExecutionSpecification,
    profile: EvalScopeVersionProfile,
) -> BenchmarkPreview:
    """Return deterministic resource impact and execution risk without starting a Job."""
    traffic = specification.traffic
    is_open_loop = isinstance(traffic, OpenLoopSweepTraffic) or (
        isinstance(traffic, SlaSearchTraffic) and isinstance(traffic.search, RateSearchRange)
    )
    risk = RiskLevel.L2 if is_open_loop else RiskLevel.L1
    return BenchmarkPreview(
        compiled=compile_benchmark(specification, profile),
        execution_risk=risk,
        requires_human_approval=is_open_loop,
    )
