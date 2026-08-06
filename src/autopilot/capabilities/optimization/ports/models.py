"""Typed Optimization Trial ledger commands and persisted snapshots."""

from typing import Literal, Self

from pydantic import Field, model_validator

from autopilot.capabilities.evidence.domain.models import EvidenceRunRef
from autopilot.capabilities.optimization.domain.enums import TrialExecutionStage
from autopilot.domain.base import NonEmptyStr, StrictModel, UtcDatetime
from autopilot.domain.enums import TrialStatus
from autopilot.domain.identifiers import (
    BenchmarkRunId,
    ExperimentId,
    PlanHash,
    PlanId,
    StudyId,
)
from autopilot.domain.trials import TrialRecord

INVALID_TRIAL_DRAFT = "Optimization Trial draft must contain one suggested Trial"
INVALID_TRIAL_COMPLETION = "Optimization Trial completion must contain terminal evidence"
INVALID_TRIAL_ENTRY = "Optimization Trial ledger fields do not match its lifecycle status"
INVALID_TRIAL_TIMELINE = "Optimization Trial ledger timestamps are not monotonic"


class TrialBudgetReservation(StrictModel):
    """Worst-case benchmark usage reserved before a Trial starts."""

    schema_version: Literal["trial-budget-reservation/v1"] = "trial-budget-reservation/v1"
    requests: int = Field(ge=1, le=10_000)
    duration_seconds: int = Field(ge=1, le=1_800)
    input_tokens: int = Field(ge=1, le=50_000_000)
    output_tokens: int = Field(ge=1, le=50_000_000)


class OptimizationTrialKey(StrictModel):
    schema_version: Literal["optimization-trial-key/v1"] = "optimization-trial-key/v1"
    experiment_id: ExperimentId
    study_id: StudyId
    trial_number: int = Field(ge=0, le=1_000_000)


class OptimizationTrialDraft(StrictModel):
    schema_version: Literal["optimization-trial-draft/v1"] = "optimization-trial-draft/v1"
    experiment_id: ExperimentId
    plan_id: PlanId
    plan_hash: PlanHash
    trial: TrialRecord
    benchmark_run_id: BenchmarkRunId
    reservation: TrialBudgetReservation

    @model_validator(mode="after")
    def validate_suggested(self) -> Self:
        if self.trial.status is not TrialStatus.SUGGESTED:
            raise ValueError(INVALID_TRIAL_DRAFT)
        return self

    def key(self) -> OptimizationTrialKey:
        return OptimizationTrialKey(
            experiment_id=self.experiment_id,
            study_id=self.trial.study_id,
            trial_number=self.trial.trial_number,
        )


class OptimizationTrialCheckpoint(StrictModel):
    schema_version: Literal["optimization-trial-checkpoint/v1"] = "optimization-trial-checkpoint/v1"
    stage: TrialExecutionStage
    provider_resource_id: NonEmptyStr


class OptimizationTrialCompletion(StrictModel):
    schema_version: Literal["optimization-trial-completion/v1"] = "optimization-trial-completion/v1"
    trial: TrialRecord
    evidence_run: EvidenceRunRef

    @model_validator(mode="after")
    def validate_terminal(self) -> Self:
        if (
            self.trial.status is TrialStatus.SUGGESTED
            or self.evidence_run.trial_id != self.trial.trial_id
        ):
            raise ValueError(INVALID_TRIAL_COMPLETION)
        return self


class OptimizationTrialEntry(StrictModel):
    """Complete application ledger record used for restart reconciliation."""

    schema_version: Literal["optimization-trial-entry/v1"] = "optimization-trial-entry/v1"
    experiment_id: ExperimentId
    plan_id: PlanId
    plan_hash: PlanHash
    trial: TrialRecord
    benchmark_run_id: BenchmarkRunId
    reservation: TrialBudgetReservation
    checkpoint: OptimizationTrialCheckpoint | None = None
    evidence_run: EvidenceRunRef | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    ended_at: UtcDatetime | None = None
    version: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        if self.updated_at < self.created_at or (
            self.ended_at is not None and self.ended_at < self.updated_at
        ):
            raise ValueError(INVALID_TRIAL_TIMELINE)
        if self.trial.status is TrialStatus.SUGGESTED:
            if self.evidence_run is not None or self.ended_at is not None:
                raise ValueError(INVALID_TRIAL_ENTRY)
        elif (
            self.checkpoint is not None
            or self.evidence_run is None
            or self.ended_at is None
            or self.evidence_run.trial_id != self.trial.trial_id
        ):
            raise ValueError(INVALID_TRIAL_ENTRY)
        return self

    def key(self) -> OptimizationTrialKey:
        return OptimizationTrialKey(
            experiment_id=self.experiment_id,
            study_id=self.trial.study_id,
            trial_number=self.trial.trial_number,
        )
