"""Versioned deterministic Champion policy."""

from typing import Literal

from pydantic import Field

from autopilot.domain.base import StrictModel


class ChampionPolicy(StrictModel):
    schema_version: Literal["champion-policy/v1"] = "champion-policy/v1"
    top_candidate_count: Literal[3] = 3
    verification_repeats: int = Field(ge=2, le=20)
    max_coefficient_of_variation: float = Field(gt=0, le=1)
    noise_multiplier: float = Field(ge=1, le=5)
    minimum_relative_improvement: float = Field(gt=0, le=1)
