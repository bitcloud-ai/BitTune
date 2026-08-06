"""Strict Agent inputs owned by the benchmark capability."""

from typing import Literal

from autopilot.capabilities.benchmark.domain.models import BenchmarkExecutionSpecification
from autopilot.domain.base import StrictModel


class CreateBenchmarkPlanInput(StrictModel):
    schema_version: Literal["create-benchmark-plan-input/v1"] = "create-benchmark-plan-input/v1"
    specification: BenchmarkExecutionSpecification


__all__ = ["CreateBenchmarkPlanInput"]
