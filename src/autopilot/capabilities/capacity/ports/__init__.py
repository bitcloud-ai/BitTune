"""Ports owned by the capacity capability."""

from typing import Protocol

from pydantic import BaseModel

from autopilot.capabilities.capacity.domain.models import (
    CapacityPlan,
    CapacityPlannerVersionProfile,
    CapacityPlanningSpecification,
    PlannerRawEstimate,
)
from autopilot.capabilities.capacity.ports.lifecycle import (
    CapacityPlannerExecutionContext,
    CapacityPlannerOperation,
    PlannerArtifactLocation,
)
from autopilot.domain.artifacts import ArtifactRef


class CapacityArtifactSink(Protocol):
    """Persist planner input/output evidence without exposing storage paths."""

    def write_capacity_artifact(
        self,
        payload: BaseModel,
        *,
        adapter_version: str,
    ) -> ArtifactRef: ...


class PlannerExecutionClient(Protocol):
    """Typed boundary for the Host Runner's fixed Planner container."""

    def estimate(
        self,
        specification: CapacityPlanningSpecification,
        profile: CapacityPlannerVersionProfile,
    ) -> PlannerRawEstimate: ...


class CapacityPlannerAdapter(Protocol):
    """Provider adapter contract used by the deterministic application layer."""

    @property
    def profile(self) -> CapacityPlannerVersionProfile: ...

    def validate(self, specification: CapacityPlanningSpecification) -> None: ...

    def create_plan(self, specification: CapacityPlanningSpecification) -> CapacityPlan: ...


class CapacityPlannerArtifactLocator(Protocol):
    def locate_for_runner(self, artifact: ArtifactRef) -> PlannerArtifactLocation: ...


class AsyncCapacityPlannerAdapter(Protocol):
    @property
    def profile(self) -> CapacityPlannerVersionProfile: ...

    def start(
        self,
        specification: CapacityPlanningSpecification,
        context: CapacityPlannerExecutionContext,
    ) -> CapacityPlannerOperation: ...
