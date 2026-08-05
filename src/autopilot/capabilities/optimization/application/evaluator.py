"""Evaluate normalized metrics and rank feasible measured Trials."""

from autopilot.capabilities.benchmark.domain.models import BenchmarkResult
from autopilot.capabilities.optimization.domain.enums import OptimizationValidationCode
from autopilot.capabilities.optimization.domain.errors import OptimizationValidationError
from autopilot.domain.constraints import NumericConstraint, SloSpec
from autopilot.domain.enums import BooleanMetric, NumericMetric, NumericOperator, TrialStatus
from autopilot.domain.identifiers import CandidateId
from autopilot.domain.trials import (
    BooleanMetricValue,
    ConstraintEvaluation,
    MetricValue,
    NumericMetricValue,
    TrialRecord,
)

TOP_CANDIDATE_COUNT = 3


def numeric_metric_value(result: BenchmarkResult, metric: NumericMetric) -> float:
    """Resolve every normalized numeric metric without provider field names."""
    values = {
        NumericMetric.E2E_P50_MS: result.latency.e2e_ms.p50,
        NumericMetric.E2E_P95_MS: result.latency.e2e_ms.p95,
        NumericMetric.E2E_P99_MS: result.latency.e2e_ms.p99,
        NumericMetric.TTFT_P50_MS: result.latency.ttft_ms.p50,
        NumericMetric.TTFT_P95_MS: result.latency.ttft_ms.p95,
        NumericMetric.TTFT_P99_MS: result.latency.ttft_ms.p99,
        NumericMetric.TPOT_P50_MS: result.latency.tpot_ms.p50,
        NumericMetric.TPOT_P95_MS: result.latency.tpot_ms.p95,
        NumericMetric.TPOT_P99_MS: result.latency.tpot_ms.p99,
        NumericMetric.ITL_P50_MS: result.latency.itl_ms.p50,
        NumericMetric.ITL_P95_MS: result.latency.itl_ms.p95,
        NumericMetric.ITL_P99_MS: result.latency.itl_ms.p99,
        NumericMetric.REQUESTS_PER_SECOND: result.throughput.requests_per_second,
        NumericMetric.SUCCESSFUL_REQUESTS_PER_MINUTE: (
            result.throughput.successful_requests_per_minute
        ),
        NumericMetric.INPUT_TOKENS_PER_SECOND: result.throughput.input_tokens_per_second,
        NumericMetric.SUCCESSFUL_OUTPUT_TOKENS_PER_SECOND: (
            result.throughput.successful_output_tokens_per_second
        ),
        NumericMetric.TOTAL_TOKENS_PER_MINUTE: result.throughput.total_tokens_per_minute,
        NumericMetric.SUCCESS_RATE: result.reliability.success_rate,
        NumericMetric.WINDOW_COMPLETION_RATIO: result.reliability.window_completion_ratio,
    }
    return values[metric]


def evaluate_slo(result: BenchmarkResult, slo: SloSpec) -> tuple[ConstraintEvaluation, ...]:
    """Evaluate hard constraints from measured normalized metrics."""
    evaluations = []
    for constraint in slo.constraints:
        observed: MetricValue
        if isinstance(constraint, NumericConstraint):
            observed = NumericMetricValue(
                metric=constraint.metric,
                value=numeric_metric_value(result, constraint.metric),
            )
            passed = (
                observed.value <= constraint.value
                if constraint.operator is NumericOperator.LESS_THAN_OR_EQUAL
                else observed.value >= constraint.value
            )
        else:
            observed = BooleanMetricValue(metric=BooleanMetric.OOM, value=result.oom)
            passed = observed.value is constraint.value
        evaluations.append(
            ConstraintEvaluation(
                constraint=constraint,
                observed=observed,
                passed=passed,
            )
        )
    return tuple(evaluations)


def objective_value(result: BenchmarkResult) -> NumericMetricValue:
    """Extract the single fixed MVP objective."""
    metric = NumericMetric.SUCCESSFUL_OUTPUT_TOKENS_PER_SECOND
    return NumericMetricValue(metric=metric, value=numeric_metric_value(result, metric))


def select_top_trial_candidates(trials: tuple[TrialRecord, ...]) -> tuple[CandidateId, ...]:
    """Return exactly the top three evidence-complete feasible measured candidates."""
    feasible = [
        trial
        for trial in trials
        if trial.status is TrialStatus.COMPLETED
        and trial.objective is not None
        and trial.provenance is not None
        and trial.evidence
        and trial.constraints
        and all(evaluation.passed for evaluation in trial.constraints)
    ]
    feasible.sort(
        key=lambda trial: (
            -trial.objective.value if trial.objective is not None else 0,
            str(trial.candidate_id),
        )
    )
    unique_candidates = tuple(dict.fromkeys(trial.candidate_id for trial in feasible))
    if len(unique_candidates) < TOP_CANDIDATE_COUNT:
        raise OptimizationValidationError(
            OptimizationValidationCode.INSUFFICIENT_FEASIBLE_TRIALS,
            "trials",
            "at least three evidence-complete feasible Trials are required",
        )
    return unique_candidates[:TOP_CANDIDATE_COUNT]
