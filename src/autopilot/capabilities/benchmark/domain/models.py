"""Versioned benchmark traffic, profile, compiled, and result contracts."""

from decimal import ROUND_CEILING, Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from autopilot.capabilities.benchmark.domain.enums import LatencyUnit, SlaSearchVariable
from autopilot.domain.base import NonEmptyStr, StrictModel
from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.constraints import SloSpec, validate_workload_against_slo
from autopilot.domain.enums import (
    BooleanMetric,
    BooleanOperator,
    NumericMetric,
    NumericOperator,
    TrafficMode,
)
from autopilot.domain.identifiers import DeploymentId, PlanHash, Sha256Digest
from autopilot.domain.plans import ExecutionSpecification
from autopilot.domain.provenance import MeasuredProvenance
from autopilot.domain.workloads import WorkloadSpec

ProviderFieldName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,127}$"),
]
PositiveRate = Annotated[float, Field(gt=0, le=10_000)]
PositiveConcurrency = Annotated[int, Field(ge=1, le=1_024)]
RequestCount = Annotated[int, Field(ge=1, le=10_000)]
INVALID_SWEEP = "benchmark sweep values must be unique and strictly increasing"
INVALID_SEARCH_RANGE = "SLA search upper bound must be greater than its lower bound"
INVALID_COMPILED_TRAFFIC = "compiled traffic arrays must have matching lengths"
INVALID_PROFILE_BINDINGS = "EvalScope profile bindings must be unique"
INVALID_RAW_COUNTS = "benchmark result request counts are inconsistent"
INVALID_PERCENTILES = "metric percentiles must be non-decreasing"
INVALID_RESULT_PROVENANCE = "benchmark result fields do not match measured provenance"
INVALID_COMPILED_BUDGET = "compiled benchmark budget estimate is inconsistent"


class BaselineTraffic(StrictModel):
    mode: Literal[TrafficMode.BASELINE] = TrafficMode.BASELINE
    requests: int = Field(ge=1, le=10_000)


class ClosedLoopSweepTraffic(StrictModel):
    mode: Literal[TrafficMode.CLOSED_LOOP_SWEEP] = TrafficMode.CLOSED_LOOP_SWEEP
    concurrency_levels: tuple[PositiveConcurrency, ...] = Field(min_length=1, max_length=16)
    requests_per_worker: int = Field(ge=1, le=1_000)

    @model_validator(mode="after")
    def validate_levels(self) -> Self:
        if tuple(sorted(set(self.concurrency_levels))) != self.concurrency_levels:
            raise ValueError(INVALID_SWEEP)
        return self


class OpenLoopSweepTraffic(StrictModel):
    mode: Literal[TrafficMode.OPEN_LOOP_SWEEP] = TrafficMode.OPEN_LOOP_SWEEP
    request_rates: tuple[PositiveRate, ...] = Field(min_length=1, max_length=16)
    duration_seconds: int = Field(ge=1, le=1_800)

    @model_validator(mode="after")
    def validate_rates(self) -> Self:
        if tuple(sorted(set(self.request_rates))) != self.request_rates:
            raise ValueError(INVALID_SWEEP)
        return self


class RateSearchRange(StrictModel):
    variable: Literal[SlaSearchVariable.RATE] = SlaSearchVariable.RATE
    lower_bound: PositiveRate
    upper_bound: PositiveRate
    duration_seconds: int = Field(ge=1, le=1_800)

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.upper_bound <= self.lower_bound:
            raise ValueError(INVALID_SEARCH_RANGE)
        return self


class ConcurrencySearchRange(StrictModel):
    variable: Literal[SlaSearchVariable.CONCURRENCY] = SlaSearchVariable.CONCURRENCY
    lower_bound: PositiveConcurrency
    upper_bound: PositiveConcurrency
    requests_per_worker: int = Field(ge=1, le=1_000)

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.upper_bound <= self.lower_bound:
            raise ValueError(INVALID_SEARCH_RANGE)
        return self


SlaSearchRange = Annotated[
    RateSearchRange | ConcurrencySearchRange,
    Field(discriminator="variable"),
]


class SlaSearchTraffic(StrictModel):
    mode: Literal[TrafficMode.SLA_SEARCH] = TrafficMode.SLA_SEARCH
    search: SlaSearchRange
    runs_per_level: int = Field(ge=1, le=10)
    max_levels: int = Field(ge=2, le=20)


TrafficSpec = Annotated[
    BaselineTraffic | ClosedLoopSweepTraffic | OpenLoopSweepTraffic | SlaSearchTraffic,
    Field(discriminator="mode"),
]


class BenchmarkExecutionSpecification(ExecutionSpecification):
    schema_version: Literal["benchmark-execution-specification/v1"] = (
        "benchmark-execution-specification/v1"
    )
    provider: Literal["evalscope"] = "evalscope"
    deployment_id: DeploymentId
    deployment_plan_hash: PlanHash
    workload: WorkloadSpec
    slo: SloSpec
    traffic: TrafficSpec

    @model_validator(mode="after")
    def validate_measurement_semantics(self) -> Self:
        validate_workload_against_slo(self.workload, self.slo)
        return self


class EvalScopeMetricBinding(StrictModel):
    metric: NumericMetric | BooleanMetric
    provider_name: ProviderFieldName


class PercentileFieldBindings(StrictModel):
    p50: ProviderFieldName
    p95: ProviderFieldName
    p99: ProviderFieldName


class LatencyFieldBindings(StrictModel):
    e2e: PercentileFieldBindings
    ttft: PercentileFieldBindings
    tpot: PercentileFieldBindings
    itl: PercentileFieldBindings


class LengthFieldBindings(StrictModel):
    input_tokens: PercentileFieldBindings
    output_tokens: PercentileFieldBindings


class ReliabilityFieldBindings(StrictModel):
    submitted: ProviderFieldName
    completed: ProviderFieldName
    failed: ProviderFieldName
    timed_out: ProviderFieldName
    completed_within_window: ProviderFieldName
    scheduled_window_seconds: ProviderFieldName
    measurement_duration_seconds: ProviderFieldName


class TokenFieldBindings(StrictModel):
    successful_input_tokens: ProviderFieldName
    successful_output_tokens: ProviderFieldName


class EvalScopeRawMetricBindings(StrictModel):
    reliability: ReliabilityFieldBindings
    tokens: TokenFieldBindings
    latency: LatencyFieldBindings
    lengths: LengthFieldBindings


class EvalScopeVersionProfile(StrictModel):
    schema_version: Literal["evalscope-version-profile/v1"] = "evalscope-version-profile/v1"
    profile_version: NonEmptyStr
    provider_version: NonEmptyStr
    adapter_version: NonEmptyStr
    rtx_5090_verified: Literal[True]
    number_safety_factor: float = Field(ge=1, le=2)
    warmup_ratio: float = Field(ge=0, le=0.5)
    rest_between_levels_seconds: int = Field(ge=0, le=300)
    closed_loop_level_timeout_seconds: int = Field(ge=1, le=1_800)
    completion_grace_seconds: int = Field(ge=0, le=300)
    max_request_rate_rps: PositiveRate
    max_closed_loop_concurrency: PositiveConcurrency
    sla_rate_parameter: ProviderFieldName
    sla_concurrency_parameter: ProviderFieldName
    sla_metric_bindings: tuple[EvalScopeMetricBinding, ...] = Field(min_length=1, max_length=32)
    raw_metric_bindings: EvalScopeRawMetricBindings
    latency_unit: LatencyUnit

    @model_validator(mode="after")
    def validate_bindings(self) -> Self:
        metrics = [binding.metric for binding in self.sla_metric_bindings]
        names = [binding.provider_name for binding in self.sla_metric_bindings]
        if len(metrics) != len(set(metrics)) or len(names) != len(set(names)):
            raise ValueError(INVALID_PROFILE_BINDINGS)
        raw_names = raw_metric_names(self.raw_metric_bindings)
        if len(raw_names) != len(set(raw_names)):
            raise ValueError(INVALID_PROFILE_BINDINGS)
        return self


class CompiledNumericConstraint(StrictModel):
    kind: Literal["numeric"] = "numeric"
    provider_metric: ProviderFieldName
    operator: NumericOperator
    value: float = Field(ge=0)


class CompiledBooleanConstraint(StrictModel):
    kind: Literal["boolean"] = "boolean"
    provider_metric: ProviderFieldName
    operator: BooleanOperator
    value: bool


CompiledConstraint = Annotated[
    CompiledNumericConstraint | CompiledBooleanConstraint,
    Field(discriminator="kind"),
]


class CompiledBaselineTraffic(StrictModel):
    mode: Literal[TrafficMode.BASELINE] = TrafficMode.BASELINE
    parallel: tuple[Literal[1]]
    number: tuple[RequestCount]
    rate: Literal[-1] = -1
    open_loop: Literal[False] = False


class CompiledClosedLoopTraffic(StrictModel):
    mode: Literal[TrafficMode.CLOSED_LOOP_SWEEP] = TrafficMode.CLOSED_LOOP_SWEEP
    parallel: tuple[PositiveConcurrency, ...] = Field(min_length=1, max_length=16)
    number: tuple[RequestCount, ...] = Field(min_length=1, max_length=16)
    rate: Literal[-1] = -1
    open_loop: Literal[False] = False

    @model_validator(mode="after")
    def validate_lengths(self) -> Self:
        if (
            len(self.parallel) != len(self.number)
            or tuple(sorted(set(self.parallel))) != self.parallel
        ):
            raise ValueError(INVALID_COMPILED_TRAFFIC)
        return self


class CompiledOpenLoopTraffic(StrictModel):
    mode: Literal[TrafficMode.OPEN_LOOP_SWEEP] = TrafficMode.OPEN_LOOP_SWEEP
    rate: tuple[PositiveRate, ...] = Field(min_length=1, max_length=16)
    number: tuple[RequestCount, ...] = Field(min_length=1, max_length=16)
    duration_seconds: int = Field(ge=1, le=1_800)
    open_loop: Literal[True] = True

    @model_validator(mode="after")
    def validate_lengths(self) -> Self:
        if len(self.rate) != len(self.number) or tuple(sorted(set(self.rate))) != self.rate:
            raise ValueError(INVALID_COMPILED_TRAFFIC)
        return self


class CompiledRateSearch(StrictModel):
    variable: Literal[SlaSearchVariable.RATE] = SlaSearchVariable.RATE
    provider_search_parameter: ProviderFieldName
    lower_bound: PositiveRate
    upper_bound: PositiveRate
    duration_seconds: int = Field(ge=1, le=1_800)
    number_per_run: RequestCount
    open_loop: Literal[True] = True

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.upper_bound <= self.lower_bound:
            raise ValueError(INVALID_SEARCH_RANGE)
        return self


class CompiledConcurrencySearch(StrictModel):
    variable: Literal[SlaSearchVariable.CONCURRENCY] = SlaSearchVariable.CONCURRENCY
    provider_search_parameter: ProviderFieldName
    lower_bound: PositiveConcurrency
    upper_bound: PositiveConcurrency
    number_per_run: RequestCount
    open_loop: Literal[False] = False

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.upper_bound <= self.lower_bound:
            raise ValueError(INVALID_SEARCH_RANGE)
        return self


CompiledSlaSearch = Annotated[
    CompiledRateSearch | CompiledConcurrencySearch,
    Field(discriminator="variable"),
]


class CompiledSlaSearchTraffic(StrictModel):
    mode: Literal[TrafficMode.SLA_SEARCH] = TrafficMode.SLA_SEARCH
    search: CompiledSlaSearch
    runs_per_level: int = Field(ge=1, le=10)
    max_levels: int = Field(ge=2, le=20)
    constraints: tuple[CompiledConstraint, ...] = Field(min_length=1, max_length=16)


CompiledTraffic = Annotated[
    CompiledBaselineTraffic
    | CompiledClosedLoopTraffic
    | CompiledOpenLoopTraffic
    | CompiledSlaSearchTraffic,
    Field(discriminator="mode"),
]


def _compiled_measurement_requests_by_run(traffic: CompiledTraffic) -> tuple[int, ...]:
    if isinstance(traffic, CompiledSlaSearchTraffic):
        run_count = traffic.runs_per_level * traffic.max_levels
        return (traffic.search.number_per_run,) * run_count
    return traffic.number


def _ceil_product(*values: int | float) -> int:
    product = Decimal(1)
    for value in values:
        product *= Decimal(str(value))
    return int(product.to_integral_value(rounding=ROUND_CEILING))


class BenchmarkBudgetEstimate(StrictModel):
    measurement_requests: int = Field(ge=1)
    warmup_requests: int = Field(ge=0)
    total_requests: int = Field(ge=1)
    estimated_duration_seconds: int = Field(ge=1)
    estimated_input_tokens: int = Field(ge=1)
    estimated_output_tokens: int = Field(ge=1)


class CompiledEvalScopeBenchmark(StrictModel):
    schema_version: Literal["compiled-evalscope-benchmark/v1"] = "compiled-evalscope-benchmark/v1"
    provider: Literal["evalscope"] = "evalscope"
    provider_version: NonEmptyStr
    adapter_version: NonEmptyStr
    provider_profile_version: NonEmptyStr
    api: Literal["openai_chat_completions"] = "openai_chat_completions"
    deployment_id: DeploymentId
    deployment_plan_hash: PlanHash
    workload: WorkloadSpec
    traffic: CompiledTraffic
    warmup_ratio: float = Field(ge=0, le=0.5)
    rest_between_levels_seconds: int = Field(ge=0, le=300)
    execution_budget: ExecutionBudget
    budget_estimate: BenchmarkBudgetEstimate

    @model_validator(mode="after")
    def validate_budget_estimate(self) -> Self:
        requests_by_run = _compiled_measurement_requests_by_run(self.traffic)
        measurement_requests = sum(requests_by_run)
        warmup_requests = sum(
            _ceil_product(requests, self.warmup_ratio) for requests in requests_by_run
        )
        total_requests = measurement_requests + warmup_requests
        minimum_duration_seconds = len(requests_by_run) + max(0, len(requests_by_run) - 1) * (
            self.rest_between_levels_seconds
        )
        if self.warmup_ratio > 0:
            minimum_duration_seconds += len(requests_by_run)
        estimate = self.budget_estimate
        budget = self.execution_budget
        if (
            estimate.measurement_requests != measurement_requests
            or estimate.warmup_requests != warmup_requests
            or estimate.total_requests != total_requests
            or estimate.estimated_input_tokens != total_requests * self.workload.prompt_tokens
            or estimate.estimated_output_tokens != total_requests * self.workload.output_tokens
            or estimate.estimated_duration_seconds < minimum_duration_seconds
            or total_requests > budget.max_requests
            or estimate.estimated_input_tokens > budget.max_input_tokens
            or estimate.estimated_output_tokens > budget.max_output_tokens
            or estimate.estimated_duration_seconds > budget.max_duration_seconds
        ):
            raise ValueError(INVALID_COMPILED_BUDGET)
        return self


class PercentileValues(StrictModel):
    p50: float = Field(ge=0)
    p95: float = Field(ge=0)
    p99: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if not self.p50 <= self.p95 <= self.p99:
            raise ValueError(INVALID_PERCENTILES)
        return self


class LatencySummary(StrictModel):
    e2e_ms: PercentileValues
    ttft_ms: PercentileValues
    tpot_ms: PercentileValues
    itl_ms: PercentileValues


class LengthSummary(StrictModel):
    input_tokens: PercentileValues
    output_tokens: PercentileValues


class ThroughputSummary(StrictModel):
    requests_per_second: float = Field(ge=0)
    successful_requests_per_minute: float = Field(ge=0)
    input_tokens_per_second: float = Field(ge=0)
    successful_output_tokens_per_second: float = Field(ge=0)
    total_tokens_per_minute: float = Field(ge=0)


class ReliabilitySummary(StrictModel):
    submitted: int = Field(ge=1)
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)
    timed_out: int = Field(ge=0)
    completed_within_window: int = Field(ge=0)
    success_rate: float = Field(ge=0, le=1)
    window_completion_ratio: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if (
            self.completed + self.failed + self.timed_out != self.submitted
            or self.completed_within_window > self.completed
        ):
            raise ValueError(INVALID_RAW_COUNTS)
        return self


class BenchmarkResult(StrictModel):
    schema_version: Literal["benchmark-result/v1"] = "benchmark-result/v1"
    provider: Literal["evalscope"] = "evalscope"
    provider_version: NonEmptyStr
    adapter_version: NonEmptyStr
    provider_profile_version: NonEmptyStr
    provider_profile_hash: Sha256Digest
    compiled_benchmark_hash: Sha256Digest
    deployment_id: DeploymentId
    deployment_plan_hash: PlanHash
    traffic_mode: TrafficMode
    scheduled_window_seconds: float = Field(gt=0)
    measurement_duration_seconds: float = Field(gt=0)
    latency: LatencySummary
    throughput: ThroughputSummary
    reliability: ReliabilitySummary
    lengths: LengthSummary
    oom: bool
    raw_report_hash: Sha256Digest
    provenance: MeasuredProvenance

    @model_validator(mode="after")
    def validate_provenance_binding(self) -> Self:
        if (
            self.provenance.provider != self.provider
            or self.provenance.provider_version != self.provider_version
            or self.provenance.adapter_version != self.adapter_version
            or self.provenance.raw_artifact.sha256 != self.raw_report_hash
        ):
            raise ValueError(INVALID_RESULT_PROVENANCE)
        return self


def raw_metric_names(bindings: EvalScopeRawMetricBindings) -> tuple[str, ...]:
    """Flatten every required raw metric name for uniqueness and completeness checks."""
    reliability = bindings.reliability
    tokens = bindings.tokens
    latency_groups = (
        bindings.latency.e2e,
        bindings.latency.ttft,
        bindings.latency.tpot,
        bindings.latency.itl,
        bindings.lengths.input_tokens,
        bindings.lengths.output_tokens,
    )
    names = [
        reliability.submitted,
        reliability.completed,
        reliability.failed,
        reliability.timed_out,
        reliability.completed_within_window,
        reliability.scheduled_window_seconds,
        reliability.measurement_duration_seconds,
        tokens.successful_input_tokens,
        tokens.successful_output_tokens,
    ]
    for group in latency_groups:
        names.extend((group.p50, group.p95, group.p99))
    return tuple(names)
