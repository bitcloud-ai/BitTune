"""Strict Agent inputs owned by the capacity capability."""

from typing import Literal

from autopilot.capabilities.capacity.domain.models import CapacityPlanningSpecification
from autopilot.domain.base import StrictModel


class CreateCapacityPlanInput(StrictModel):
    schema_version: Literal["create-capacity-plan-input/v1"] = "create-capacity-plan-input/v1"
    specification: CapacityPlanningSpecification


__all__ = ["CreateCapacityPlanInput"]
