"""Structured experiment requirements produced from user intent."""

from typing import Literal, Self

from pydantic import model_validator

from autopilot.domain.base import StrictModel
from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.constraints import SloSpec, validate_workload_against_slo
from autopilot.domain.identifiers import UserId
from autopilot.domain.models import ModelRef
from autopilot.domain.workloads import WorkloadSpec


class RequirementSpec(StrictModel):
    schema_version: Literal["requirements/v1"] = "requirements/v1"
    created_by: UserId
    model_ref: ModelRef
    priority: Literal["balanced", "latency", "throughput"]
    workload: WorkloadSpec
    slo: SloSpec
    budget: ExecutionBudget
    allow_model_download: bool
    allow_container_start: bool

    @model_validator(mode="after")
    def validate_workload_slo(self) -> Self:
        validate_workload_against_slo(self.workload, self.slo)
        return self
