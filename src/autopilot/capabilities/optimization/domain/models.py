"""Closed vLLM search-space contracts for the MVP."""

from decimal import Decimal
from typing import Literal, Self

from pydantic import Field, model_validator

from autopilot.domain.base import NonEmptyStr, StrictModel
from autopilot.domain.constraints import ObjectiveSpec, SloSpec

MAX_GPU_MEMORY_GRID_POINTS = 15
INVALID_FLOAT_RANGE = (
    "search range must be increasing, exactly divisible by its step, and contain at most 15 values"
)
INVALID_CHOICES = "search-space categorical choices must be unique and ordered"


class GpuMemoryUtilizationRange(StrictModel):
    low: float = Field(ge=0.80, le=0.94)
    high: float = Field(ge=0.80, le=0.94)
    step: float = Field(gt=0, le=0.14)

    @model_validator(mode="after")
    def validate_grid(self) -> Self:
        low = Decimal(str(self.low))
        high = Decimal(str(self.high))
        step = Decimal(str(self.step))
        distance = high - low
        if (
            high <= low
            or distance % step != 0
            or int(distance / step) + 1 > MAX_GPU_MEMORY_GRID_POINTS
        ):
            raise ValueError(INVALID_FLOAT_RANGE)
        return self


class VllmSearchSpaceSpec(StrictModel):
    schema_version: Literal["vllm-search-space/v1"] = "vllm-search-space/v1"
    profile_name: NonEmptyStr
    objective: ObjectiveSpec
    slo: SloSpec
    gpu_memory_utilization: GpuMemoryUtilizationRange
    max_num_seqs: tuple[Literal[4, 8, 16, 32], ...] = Field(min_length=1, max_length=4)
    max_num_batched_tokens: tuple[Literal[2048, 4096, 8192, 16384], ...] = Field(
        min_length=1, max_length=4
    )
    enable_chunked_prefill: tuple[bool, ...] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def validate_choices(self) -> Self:
        if (
            tuple(sorted(set(self.max_num_seqs))) != self.max_num_seqs
            or tuple(sorted(set(self.max_num_batched_tokens))) != self.max_num_batched_tokens
            or tuple(sorted(set(self.enable_chunked_prefill))) != self.enable_chunked_prefill
        ):
            raise ValueError(INVALID_CHOICES)
        return self
