"""Mandatory execution budgets enforced before and during provider jobs."""

from typing import Literal, Self

from pydantic import Field, model_validator

from autopilot.domain.base import StrictModel

MAX_BENCHMARK_DURATION_SECONDS = 1_800
MAX_BENCHMARK_REQUESTS = 10_000
MAX_BENCHMARK_TOKENS = 50_000_000
MAX_DISK_GROWTH_BYTES = 20_000_000_000
TOTAL_LIMIT_EXCEEDED = "input and output token budgets exceed the benchmark limit"


class ExecutionBudget(StrictModel):
    schema_version: Literal["execution-budget/v1"] = "execution-budget/v1"
    max_duration_seconds: int = Field(ge=1, le=MAX_BENCHMARK_DURATION_SECONDS)
    max_requests: int = Field(ge=1, le=MAX_BENCHMARK_REQUESTS)
    max_input_tokens: int = Field(ge=1, le=MAX_BENCHMARK_TOKENS)
    max_output_tokens: int = Field(ge=1, le=MAX_BENCHMARK_TOKENS)
    max_disk_growth_bytes: int = Field(ge=1, le=MAX_DISK_GROWTH_BYTES)

    @model_validator(mode="after")
    def validate_total_tokens(self) -> Self:
        if self.max_input_tokens + self.max_output_tokens > MAX_BENCHMARK_TOKENS:
            raise ValueError(TOTAL_LIMIT_EXCEEDED)
        return self
