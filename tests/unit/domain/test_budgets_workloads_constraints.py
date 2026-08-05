import pytest
from pydantic import TypeAdapter, ValidationError

from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.constraints import (
    BooleanConstraint,
    Constraint,
    NumericConstraint,
    SloSpec,
    validate_workload_against_slo,
)
from autopilot.domain.enums import NumericMetric, NumericOperator
from autopilot.domain.trials import ConstraintEvaluation, NumericMetricValue
from autopilot.domain.workloads import WorkloadSpec


def test_execution_budget_rejects_combined_token_limit() -> None:
    with pytest.raises(ValidationError, match="exceed the benchmark limit"):
        ExecutionBudget(
            max_duration_seconds=600,
            max_requests=5_000,
            max_input_tokens=40_000_000,
            max_output_tokens=20_000_000,
            max_disk_growth_bytes=20_000_000_000,
        )


def test_dataset_discriminator_rejects_cross_mode_fields(workload: WorkloadSpec) -> None:
    payload = workload.model_dump(mode="json")
    payload["dataset"]["artifact"] = {"artifact_id": "artifact_invalid"}

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        WorkloadSpec.model_validate(payload)


def test_numeric_constraint_enforces_metric_operator_semantics() -> None:
    with pytest.raises(ValidationError, match="latency constraints"):
        NumericConstraint(
            metric=NumericMetric.TTFT_P95_MS,
            operator=NumericOperator.GREATER_THAN_OR_EQUAL,
            value=2_000,
        )


def test_slo_rejects_duplicate_metrics() -> None:
    constraint = NumericConstraint(
        metric=NumericMetric.TTFT_P95_MS,
        operator=NumericOperator.LESS_THAN_OR_EQUAL,
        value=2_000,
    )

    with pytest.raises(ValidationError, match="duplicate metrics"):
        SloSpec(constraints=(constraint, constraint))


def test_ttft_slo_requires_streaming(workload: WorkloadSpec) -> None:
    non_streaming = workload.model_copy(update={"stream": False})
    slo = SloSpec(
        constraints=(
            NumericConstraint(
                metric=NumericMetric.TTFT_P95_MS,
                operator=NumericOperator.LESS_THAN_OR_EQUAL,
                value=2_000,
            ),
            BooleanConstraint(),
        )
    )

    with pytest.raises(ValueError, match="streaming"):
        validate_workload_against_slo(non_streaming, slo)


def test_constraint_union_rejects_boolean_fields_in_numeric_mode() -> None:
    adapter = TypeAdapter(Constraint)

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        adapter.validate_python(
            {
                "kind": "numeric",
                "metric": "ttft_p95_ms",
                "operator": "<=",
                "value": 2_000,
                "boolean_value": False,
            }
        )


def test_constraint_evaluation_rejects_forged_pass_result() -> None:
    constraint = NumericConstraint(
        metric=NumericMetric.TTFT_P95_MS,
        operator=NumericOperator.LESS_THAN_OR_EQUAL,
        value=2_000,
    )

    with pytest.raises(ValidationError, match="does not match"):
        ConstraintEvaluation(
            constraint=constraint,
            observed=NumericMetricValue(metric=NumericMetric.TTFT_P95_MS, value=2_500),
            passed=True,
        )
