"""Typed SLO constraints and the single MVP optimization objective."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from autopilot.domain.base import StrictModel
from autopilot.domain.enums import (
    BooleanMetric,
    BooleanOperator,
    NumericMetric,
    NumericOperator,
    ObjectiveDirection,
)
from autopilot.domain.workloads import WorkloadSpec

INVALID_LATENCY_OPERATOR = "latency constraints must use the less-than-or-equal operator"
INVALID_MINIMUM_OPERATOR = (
    "throughput and reliability constraints must use the greater-than-or-equal operator"
)
INVALID_RATIO = "ratio constraints must be between zero and one"
DUPLICATE_CONSTRAINT = "an SLO cannot contain duplicate metrics"
TTFT_REQUIRES_STREAM = "TTFT constraints require a streaming workload"

LATENCY_METRICS = frozenset(
    metric
    for metric in NumericMetric
    if metric.value.startswith(("e2e_", "ttft_", "tpot_", "itl_"))
)
RATIO_METRICS = frozenset({NumericMetric.SUCCESS_RATE, NumericMetric.WINDOW_COMPLETION_RATIO})


class NumericConstraint(StrictModel):
    kind: Literal["numeric"] = "numeric"
    metric: NumericMetric
    operator: NumericOperator
    value: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_metric_semantics(self) -> Self:
        if (
            self.metric in LATENCY_METRICS
            and self.operator is not NumericOperator.LESS_THAN_OR_EQUAL
        ):
            raise ValueError(INVALID_LATENCY_OPERATOR)
        if (
            self.metric not in LATENCY_METRICS
            and self.operator is not NumericOperator.GREATER_THAN_OR_EQUAL
        ):
            raise ValueError(INVALID_MINIMUM_OPERATOR)
        if self.metric in RATIO_METRICS and self.value > 1:
            raise ValueError(INVALID_RATIO)
        return self


class BooleanConstraint(StrictModel):
    kind: Literal["boolean"] = "boolean"
    metric: Literal[BooleanMetric.OOM] = BooleanMetric.OOM
    operator: Literal[BooleanOperator.EQUAL] = BooleanOperator.EQUAL
    value: Literal[False] = False


Constraint = Annotated[NumericConstraint | BooleanConstraint, Field(discriminator="kind")]


class SloSpec(StrictModel):
    schema_version: Literal["slo/v1"] = "slo/v1"
    constraints: tuple[Constraint, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_unique_metrics(self) -> Self:
        metrics = [constraint.metric for constraint in self.constraints]
        if len(metrics) != len(set(metrics)):
            raise ValueError(DUPLICATE_CONSTRAINT)
        return self

    def requires_ttft(self) -> bool:
        return any(
            isinstance(constraint, NumericConstraint)
            and constraint.metric.value.startswith("ttft_")
            for constraint in self.constraints
        )


class ObjectiveSpec(StrictModel):
    metric: Literal[NumericMetric.SUCCESSFUL_OUTPUT_TOKENS_PER_SECOND] = (
        NumericMetric.SUCCESSFUL_OUTPUT_TOKENS_PER_SECOND
    )
    direction: Literal[ObjectiveDirection.MAXIMIZE] = ObjectiveDirection.MAXIMIZE


def validate_workload_against_slo(workload: WorkloadSpec, slo: SloSpec) -> None:
    """Reject a workload that cannot measure its declared SLO metrics."""
    if slo.requires_ttft() and not workload.stream:
        raise ValueError(TTFT_REQUIRES_STREAM)
