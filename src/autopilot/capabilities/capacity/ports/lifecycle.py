"""Typed llm-d Planner Runner lifecycle contracts."""

from typing import Annotated, Literal, Protocol

from pydantic import StringConstraints

from autopilot.capabilities.capacity.domain.models import (
    CapacityPlannerVersionProfile,
    CapacityPlanningSpecification,
    PlannerRawEstimate,
)
from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.base import NonEmptyStr, StrictModel
from autopilot.domain.identifiers import JobId, PlanHash, PlanId, Sha256Digest, WorkerId
from runner.models import PlannerRuntimeBudget

PlannerStorageKey = Annotated[
    str,
    StringConstraints(
        pattern=r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+){0,15}$",
        max_length=512,
    ),
]


class PlannerArtifactLocation(StrictModel):
    schema_version: Literal["planner-artifact-location/v1"] = "planner-artifact-location/v1"
    storage_key: PlannerStorageKey


class CapacityPlannerExecutionContext(StrictModel):
    schema_version: Literal["capacity-planner-execution-context/v1"] = (
        "capacity-planner-execution-context/v1"
    )
    job_id: JobId
    plan_id: PlanId
    plan_hash: PlanHash
    idempotency_key: Sha256Digest
    worker_id: WorkerId
    request_id: NonEmptyStr
    model_config_artifact: ArtifactRef
    hardware_passport_artifact: ArtifactRef
    model_artifact: ArtifactRef | None = None
    budget: PlannerRuntimeBudget


class CapacityPlannerOperation(StrictModel):
    schema_version: Literal["capacity-planner-operation/v1"] = "capacity-planner-operation/v1"
    job_id: JobId
    state: Literal["accepted", "running", "succeeded", "cancelled", "failed"]
    provider_resource_id: NonEmptyStr
    idempotent_replay: bool = False
    detail: NonEmptyStr | None = None


class PlannerArtifactLocator(Protocol):
    def locate_for_runner(self, artifact: ArtifactRef) -> PlannerArtifactLocation: ...


class PlannerResultReader(Protocol):
    def read_planner_result(self, job_id: JobId) -> PlannerRawEstimate: ...


class AsyncPlannerAdapter(Protocol):
    @property
    def profile(self) -> CapacityPlannerVersionProfile: ...

    def start(
        self,
        specification: CapacityPlanningSpecification,
        context: CapacityPlannerExecutionContext,
    ) -> CapacityPlannerOperation: ...

    def status(self, context: CapacityPlannerExecutionContext) -> CapacityPlannerOperation: ...

    def cancel(self, context: CapacityPlannerExecutionContext) -> CapacityPlannerOperation: ...

    def collect(self, context: CapacityPlannerExecutionContext) -> PlannerRawEstimate: ...
