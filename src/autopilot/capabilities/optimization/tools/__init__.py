"""Strict Agent inputs owned by the optimization capability."""

from typing import Literal

from autopilot.capabilities.optimization.domain.models import OptimizationExecutionSpecification
from autopilot.domain.base import StrictModel


class CreateOptimizationPlanInput(StrictModel):
    schema_version: Literal["create-optimization-plan-input/v1"] = (
        "create-optimization-plan-input/v1"
    )
    specification: OptimizationExecutionSpecification


__all__ = ["CreateOptimizationPlanInput"]
