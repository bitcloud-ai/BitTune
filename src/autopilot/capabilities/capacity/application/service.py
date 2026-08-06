"""Capacity application service."""

from autopilot.capabilities.capacity.application.compiler import compile_capacity_plan
from autopilot.capabilities.capacity.application.validator import validate_capacity_specification
from autopilot.capabilities.capacity.domain.models import (
    CapacityPlan,
    CapacityPlannerVersionProfile,
    CapacityPlanningSpecification,
    PlannerRawEstimate,
)
from autopilot.capabilities.capacity.ports import PlannerExecutionClient


class CapacityPlanningService:
    """Run the deterministic Planner boundary and compile an immutable plan."""

    def __init__(
        self,
        *,
        profile: CapacityPlannerVersionProfile,
        client: PlannerExecutionClient,
    ) -> None:
        self._profile = profile
        self._client = client

    @property
    def profile(self) -> CapacityPlannerVersionProfile:
        return self._profile

    def validate(self, specification: CapacityPlanningSpecification) -> None:
        validate_capacity_specification(specification, self._profile)

    def create_plan(self, specification: CapacityPlanningSpecification) -> CapacityPlan:
        self.validate(specification)
        raw_estimate: PlannerRawEstimate = self._client.estimate(specification, self._profile)
        return compile_capacity_plan(specification, self._profile, raw_estimate)
