"""Pure validation for immutable EvalScope benchmark specifications."""

from typing import Never

from autopilot.capabilities.benchmark.domain.enums import BenchmarkValidationCode
from autopilot.capabilities.benchmark.domain.errors import BenchmarkValidationError
from autopilot.capabilities.benchmark.domain.models import (
    BenchmarkExecutionSpecification,
    ClosedLoopSweepTraffic,
    ConcurrencySearchRange,
    EvalScopeMetricBinding,
    EvalScopeVersionProfile,
    OpenLoopSweepTraffic,
    RateSearchRange,
    SlaSearchTraffic,
)


def _reject(code: BenchmarkValidationCode, field: str, message: str) -> Never:
    raise BenchmarkValidationError(code, field, message)


def metric_binding(
    profile: EvalScopeVersionProfile,
    metric: object,
) -> EvalScopeMetricBinding:
    """Resolve one fixed metric binding or fail closed."""
    for binding in profile.sla_metric_bindings:
        if binding.metric == metric:
            return binding
    _reject(
        BenchmarkValidationCode.METRIC_UNSUPPORTED,
        "slo.constraints.metric",
        "SLO metric is not mapped by the verified EvalScope profile",
    )


def validate_benchmark_specification(
    specification: BenchmarkExecutionSpecification,
    profile: EvalScopeVersionProfile,
) -> None:
    """Validate versions, traffic bounds, and metric mappings."""
    if (
        specification.provider_version != profile.provider_version
        or specification.adapter_version != profile.adapter_version
        or specification.provider_profile_version != profile.profile_version
    ):
        _reject(
            BenchmarkValidationCode.VERSION_MISMATCH,
            "provider_version",
            "benchmark specification and verified profile versions must match",
        )
    traffic = specification.traffic
    if isinstance(traffic, ClosedLoopSweepTraffic) and (
        max(traffic.concurrency_levels) > profile.max_closed_loop_concurrency
    ):
        _reject(
            BenchmarkValidationCode.TRAFFIC_UNSUPPORTED,
            "traffic.concurrency_levels",
            "closed-loop concurrency exceeds the verified profile limit",
        )
    if isinstance(traffic, OpenLoopSweepTraffic) and (
        max(traffic.request_rates) > profile.max_request_rate_rps
    ):
        _reject(
            BenchmarkValidationCode.TRAFFIC_UNSUPPORTED,
            "traffic.request_rates",
            "open-loop request rate exceeds the verified profile limit",
        )
    if isinstance(traffic, SlaSearchTraffic):
        search = traffic.search
        if (
            isinstance(search, RateSearchRange)
            and search.upper_bound > profile.max_request_rate_rps
        ):
            _reject(
                BenchmarkValidationCode.TRAFFIC_UNSUPPORTED,
                "traffic.search.upper_bound",
                "SLA rate bound exceeds the verified profile limit",
            )
        if isinstance(search, ConcurrencySearchRange) and (
            search.upper_bound > profile.max_closed_loop_concurrency
        ):
            _reject(
                BenchmarkValidationCode.TRAFFIC_UNSUPPORTED,
                "traffic.search.upper_bound",
                "SLA concurrency bound exceeds the verified profile limit",
            )
    for constraint in specification.slo.constraints:
        metric_binding(profile, constraint.metric)
