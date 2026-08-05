import pytest
from pydantic import TypeAdapter, ValidationError

from autopilot.capabilities.benchmark.domain.models import (
    CompiledRateSearch,
    CompiledTraffic,
    TrafficSpec,
)

TRAFFIC_ADAPTER = TypeAdapter(TrafficSpec)
COMPILED_TRAFFIC_ADAPTER = TypeAdapter(CompiledTraffic)


def test_open_loop_rejects_closed_loop_fields() -> None:
    with pytest.raises(ValidationError, match="concurrency_levels"):
        TRAFFIC_ADAPTER.validate_python(
            {
                "mode": "open_loop_sweep",
                "request_rates": [1.0, 2.0],
                "duration_seconds": 60,
                "concurrency_levels": [1, 2],
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "mode": "closed_loop_sweep",
            "concurrency_levels": [1, 1, 2],
            "requests_per_worker": 5,
        },
        {
            "mode": "open_loop_sweep",
            "request_rates": [2.0, 1.0],
            "duration_seconds": 60,
        },
    ],
)
def test_sweeps_require_unique_increasing_values(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="strictly increasing"):
        TRAFFIC_ADAPTER.validate_python(payload)


def test_sla_range_requires_distinct_ordered_bounds() -> None:
    with pytest.raises(ValidationError, match="upper bound"):
        TRAFFIC_ADAPTER.validate_python(
            {
                "mode": "sla_search",
                "search": {
                    "variable": "rate",
                    "lower_bound": 5.0,
                    "upper_bound": 5.0,
                    "duration_seconds": 30,
                },
                "runs_per_level": 2,
                "max_levels": 3,
            }
        )


@pytest.mark.parametrize(
    "search",
    [
        {
            "variable": "rate",
            "lower_bound": 1.0,
            "upper_bound": 5.0,
            "duration_seconds": 30,
            "requests_per_worker": 5,
        },
        {
            "variable": "concurrency",
            "lower_bound": 1,
            "upper_bound": 8,
            "requests_per_worker": 5,
            "duration_seconds": 30,
        },
    ],
)
def test_sla_search_rejects_fields_from_the_other_traffic_mode(
    search: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        TRAFFIC_ADAPTER.validate_python(
            {
                "mode": "sla_search",
                "search": search,
                "runs_per_level": 2,
                "max_levels": 3,
            }
        )


def test_compiled_traffic_rejects_non_positive_request_counts() -> None:
    with pytest.raises(ValidationError):
        COMPILED_TRAFFIC_ADAPTER.validate_python(
            {
                "mode": "open_loop_sweep",
                "rate": [1.0],
                "number": [0],
                "duration_seconds": 60,
                "open_loop": True,
            }
        )


def test_compiled_sla_search_revalidates_bounds() -> None:
    with pytest.raises(ValidationError, match="upper bound"):
        CompiledRateSearch(
            provider_search_parameter="request_rate",
            lower_bound=5.0,
            upper_bound=5.0,
            duration_seconds=60,
            number_per_run=300,
        )
