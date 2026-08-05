"""Compile bounded benchmark plans into a closed EvalScope provider DTO."""

from decimal import ROUND_CEILING, Decimal

from autopilot.capabilities.benchmark.application.validator import (
    metric_binding,
    validate_benchmark_specification,
)
from autopilot.capabilities.benchmark.domain.enums import BenchmarkValidationCode, LatencyUnit
from autopilot.capabilities.benchmark.domain.errors import BenchmarkValidationError
from autopilot.capabilities.benchmark.domain.models import (
    BaselineTraffic,
    BenchmarkBudgetEstimate,
    BenchmarkExecutionSpecification,
    ClosedLoopSweepTraffic,
    CompiledBaselineTraffic,
    CompiledBooleanConstraint,
    CompiledClosedLoopTraffic,
    CompiledConcurrencySearch,
    CompiledConstraint,
    CompiledEvalScopeBenchmark,
    CompiledNumericConstraint,
    CompiledOpenLoopTraffic,
    CompiledRateSearch,
    CompiledSlaSearch,
    CompiledSlaSearchTraffic,
    CompiledTraffic,
    EvalScopeVersionProfile,
    OpenLoopSweepTraffic,
    RateSearchRange,
    SlaSearchTraffic,
)
from autopilot.domain.constraints import LATENCY_METRICS, NumericConstraint


def _ceil_product(*values: int | float) -> int:
    product = Decimal(1)
    for value in values:
        product *= Decimal(str(value))
    return int(product.to_integral_value(rounding=ROUND_CEILING))


def _compile_constraints(
    specification: BenchmarkExecutionSpecification,
    profile: EvalScopeVersionProfile,
) -> tuple[CompiledConstraint, ...]:
    compiled: list[CompiledConstraint] = []
    for constraint in specification.slo.constraints:
        provider_metric = metric_binding(profile, constraint.metric).provider_name
        if isinstance(constraint, NumericConstraint):
            value = constraint.value
            if constraint.metric in LATENCY_METRICS and profile.latency_unit is LatencyUnit.SECONDS:
                value /= 1_000
            compiled.append(
                CompiledNumericConstraint(
                    provider_metric=provider_metric,
                    operator=constraint.operator,
                    value=value,
                )
            )
        else:
            compiled.append(
                CompiledBooleanConstraint(
                    provider_metric=provider_metric,
                    operator=constraint.operator,
                    value=constraint.value,
                )
            )
    return tuple(compiled)


def _compile_sla_traffic(
    traffic: SlaSearchTraffic,
    specification: BenchmarkExecutionSpecification,
    profile: EvalScopeVersionProfile,
) -> tuple[CompiledSlaSearchTraffic, tuple[int, ...], tuple[int, ...]]:
    search = traffic.search
    total_runs = traffic.runs_per_level * traffic.max_levels
    compiled_search: CompiledSlaSearch
    if isinstance(search, RateSearchRange):
        number_per_run = _ceil_product(
            search.upper_bound,
            search.duration_seconds,
            profile.number_safety_factor,
        )
        compiled_search = CompiledRateSearch(
            provider_search_parameter=profile.sla_rate_parameter,
            lower_bound=search.lower_bound,
            upper_bound=search.upper_bound,
            duration_seconds=search.duration_seconds,
            number_per_run=number_per_run,
        )
        duration_per_run = search.duration_seconds + profile.completion_grace_seconds
    else:
        number_per_run = search.upper_bound * search.requests_per_worker
        compiled_search = CompiledConcurrencySearch(
            provider_search_parameter=profile.sla_concurrency_parameter,
            lower_bound=search.lower_bound,
            upper_bound=search.upper_bound,
            number_per_run=number_per_run,
        )
        duration_per_run = profile.closed_loop_level_timeout_seconds
    if number_per_run > specification.budget.max_requests:
        raise BenchmarkValidationError(
            BenchmarkValidationCode.BUDGET_EXCEEDED,
            "traffic",
            "one SLA search run exceeds the request budget",
        )
    compiled = CompiledSlaSearchTraffic(
        search=compiled_search,
        runs_per_level=traffic.runs_per_level,
        max_levels=traffic.max_levels,
        constraints=_compile_constraints(specification, profile),
    )
    return (
        compiled,
        (number_per_run,) * total_runs,
        (duration_per_run,) * total_runs,
    )


def _compile_traffic(
    specification: BenchmarkExecutionSpecification,
    profile: EvalScopeVersionProfile,
) -> tuple[CompiledTraffic, tuple[int, ...], tuple[int, ...]]:
    traffic = specification.traffic
    if isinstance(traffic, BaselineTraffic):
        return (
            CompiledBaselineTraffic(parallel=(1,), number=(traffic.requests,)),
            (traffic.requests,),
            (profile.closed_loop_level_timeout_seconds,),
        )
    if isinstance(traffic, ClosedLoopSweepTraffic):
        numbers = tuple(
            concurrency * traffic.requests_per_worker for concurrency in traffic.concurrency_levels
        )
        levels = len(traffic.concurrency_levels)
        return (
            CompiledClosedLoopTraffic(
                parallel=traffic.concurrency_levels,
                number=numbers,
            ),
            numbers,
            (profile.closed_loop_level_timeout_seconds,) * levels,
        )
    if isinstance(traffic, OpenLoopSweepTraffic):
        numbers = tuple(
            _ceil_product(rate, traffic.duration_seconds, profile.number_safety_factor)
            for rate in traffic.request_rates
        )
        levels = len(traffic.request_rates)
        return (
            CompiledOpenLoopTraffic(
                rate=traffic.request_rates,
                number=numbers,
                duration_seconds=traffic.duration_seconds,
            ),
            numbers,
            (traffic.duration_seconds + profile.completion_grace_seconds,) * levels,
        )
    return _compile_sla_traffic(traffic, specification, profile)


def _estimate_budget(
    specification: BenchmarkExecutionSpecification,
    profile: EvalScopeVersionProfile,
    measurement_requests_by_run: tuple[int, ...],
    measurement_duration_by_run: tuple[int, ...],
) -> BenchmarkBudgetEstimate:
    measurement_requests = sum(measurement_requests_by_run)
    measurement_duration_seconds = sum(measurement_duration_by_run)
    warmup_requests = sum(
        _ceil_product(requests, profile.warmup_ratio) for requests in measurement_requests_by_run
    )
    total_requests = measurement_requests + warmup_requests
    estimated_input_tokens = total_requests * specification.workload.prompt_tokens
    estimated_output_tokens = total_requests * specification.workload.output_tokens
    rest_seconds = max(0, len(measurement_requests_by_run) - 1) * (
        profile.rest_between_levels_seconds
    )
    warmup_duration_seconds = sum(
        _ceil_product(duration, profile.warmup_ratio) for duration in measurement_duration_by_run
    )
    estimated_duration_seconds = (
        measurement_duration_seconds + warmup_duration_seconds + rest_seconds
    )
    budget = specification.budget
    if (
        total_requests > budget.max_requests
        or estimated_input_tokens > budget.max_input_tokens
        or estimated_output_tokens > budget.max_output_tokens
        or estimated_duration_seconds > budget.max_duration_seconds
    ):
        raise BenchmarkValidationError(
            BenchmarkValidationCode.BUDGET_EXCEEDED,
            "budget",
            "compiled benchmark exceeds its request, token, or duration budget",
        )
    return BenchmarkBudgetEstimate(
        measurement_requests=measurement_requests,
        warmup_requests=warmup_requests,
        total_requests=total_requests,
        estimated_duration_seconds=estimated_duration_seconds,
        estimated_input_tokens=estimated_input_tokens,
        estimated_output_tokens=estimated_output_tokens,
    )


def compile_benchmark(
    specification: BenchmarkExecutionSpecification,
    profile: EvalScopeVersionProfile,
) -> CompiledEvalScopeBenchmark:
    """Compile and budget a benchmark without performing provider or endpoint I/O."""
    validate_benchmark_specification(specification, profile)
    traffic, requests_by_run, durations_by_run = _compile_traffic(specification, profile)
    budget_estimate = _estimate_budget(
        specification,
        profile,
        requests_by_run,
        durations_by_run,
    )
    return CompiledEvalScopeBenchmark(
        provider_version=profile.provider_version,
        adapter_version=profile.adapter_version,
        provider_profile_version=profile.profile_version,
        deployment_id=specification.deployment_id,
        deployment_plan_hash=specification.deployment_plan_hash,
        workload=specification.workload,
        traffic=traffic,
        warmup_ratio=profile.warmup_ratio,
        rest_between_levels_seconds=profile.rest_between_levels_seconds,
        execution_budget=specification.budget,
        budget_estimate=budget_estimate,
    )
