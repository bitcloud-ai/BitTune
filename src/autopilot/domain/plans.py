"""Immutable versioned Plan envelopes and execution requests."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import SerializeAsAny, model_validator

from autopilot.domain.base import NonEmptyStr, SchemaVersion, StrictModel, UtcDatetime
from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.enums import PlanKind, PlanStatus, RiskLevel
from autopilot.domain.hashing import compute_plan_hash
from autopilot.domain.identifiers import ExperimentId, PlanHash, PlanId

PLAN_HASH_MISMATCH = "plan hash does not match the immutable execution specification"


class ExecutionSpecification(StrictModel):
    schema_version: SchemaVersion
    provider: NonEmptyStr
    provider_version: NonEmptyStr
    adapter_version: NonEmptyStr
    provider_profile_version: NonEmptyStr
    budget: ExecutionBudget


class PlanHashMaterial[ExecutionSpecificationT: ExecutionSpecification](StrictModel):
    schema_version: Literal["plan-hash-material/v1"] = "plan-hash-material/v1"
    plan_id: PlanId
    experiment_id: ExperimentId
    kind: PlanKind
    risk_level: RiskLevel
    execution_specification: SerializeAsAny[ExecutionSpecificationT]


def compute_plan_envelope_hash[ExecutionSpecificationT: ExecutionSpecification](
    *,
    plan_id: PlanId,
    experiment_id: ExperimentId,
    kind: PlanKind,
    risk_level: RiskLevel,
    execution_specification: ExecutionSpecificationT,
) -> PlanHash:
    """Hash every immutable, approval-relevant Plan field."""
    material = PlanHashMaterial[ExecutionSpecificationT](
        plan_id=plan_id,
        experiment_id=experiment_id,
        kind=kind,
        risk_level=risk_level,
        execution_specification=execution_specification,
    )
    return compute_plan_hash(material)


class PlanEnvelope[ExecutionSpecificationT: ExecutionSpecification](StrictModel):
    schema_version: Literal["plan-envelope/v1"] = "plan-envelope/v1"
    plan_id: PlanId
    experiment_id: ExperimentId
    kind: PlanKind
    status: PlanStatus = PlanStatus.DRAFT
    risk_level: RiskLevel
    execution_specification: SerializeAsAny[ExecutionSpecificationT]
    plan_hash: PlanHash
    created_at: UtcDatetime

    @model_validator(mode="after")
    def validate_plan_hash(self) -> Self:
        expected = compute_plan_envelope_hash(
            plan_id=self.plan_id,
            experiment_id=self.experiment_id,
            kind=self.kind,
            risk_level=self.risk_level,
            execution_specification=self.execution_specification,
        )
        if expected != self.plan_hash:
            raise ValueError(PLAN_HASH_MISMATCH)
        return self


class PlanExecutionRequest(StrictModel):
    schema_version: Literal["plan-execution-request/v1"] = "plan-execution-request/v1"
    plan_id: PlanId
    expected_plan_hash: PlanHash
