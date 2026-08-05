"""Typed optimization Trial, verification, and Champion evidence contracts."""

import math
import statistics
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.base import StrictModel
from autopilot.domain.candidates import VllmTuningSpec
from autopilot.domain.constraints import Constraint, NumericConstraint, ObjectiveSpec
from autopilot.domain.enums import BooleanMetric, NumericMetric, NumericOperator, TrialStatus
from autopilot.domain.errors import ErrorEnvelope
from autopilot.domain.identifiers import CandidateId, StudyId, TrialId
from autopilot.domain.provenance import DerivedProvenance, MeasuredProvenance

INCOMPLETE_TRIAL_RESULT = "completed Trial requires objective, provenance, and evidence"
MISSING_TRIAL_ERROR = "failed Trial requires a typed error"
INVALID_TRIAL_STATE_DATA = "Trial data does not match its lifecycle status"
METRIC_MISMATCH = "constraint and observed metric must match"
INVALID_CONSTRAINT_RESULT = "Trial status does not match constraint evaluation"
INVALID_CHAMPION = "Champion and fallback must be distinct feasible verified candidates"
INVALID_OBJECTIVE_METRIC = "Trial objective must use successful output tokens per second"
INVALID_VERIFICATION_STATISTICS = "verification statistics do not match repeat values"
UNSTABLE_CHAMPION = "verified candidate variation exceeds the Champion policy threshold"

PositiveMetric = Annotated[float, Field(gt=0)]


class NumericMetricValue(StrictModel):
    kind: Literal["numeric"] = "numeric"
    metric: NumericMetric
    value: float = Field(ge=0)


class BooleanMetricValue(StrictModel):
    kind: Literal["boolean"] = "boolean"
    metric: BooleanMetric
    value: bool


MetricValue = Annotated[NumericMetricValue | BooleanMetricValue, Field(discriminator="kind")]


class ConstraintEvaluation(StrictModel):
    constraint: Constraint
    observed: MetricValue
    passed: bool

    @model_validator(mode="after")
    def validate_metric(self) -> Self:
        if self.constraint.metric != self.observed.metric:
            raise ValueError(METRIC_MISMATCH)
        if isinstance(self.constraint, NumericConstraint) and isinstance(
            self.observed, NumericMetricValue
        ):
            expected = (
                self.observed.value <= self.constraint.value
                if self.constraint.operator is NumericOperator.LESS_THAN_OR_EQUAL
                else self.observed.value >= self.constraint.value
            )
        elif isinstance(self.observed, BooleanMetricValue):
            expected = self.observed.value is self.constraint.value
        else:
            raise ValueError(METRIC_MISMATCH)  # noqa: TRY004
        if self.passed is not expected:
            raise ValueError(INVALID_CONSTRAINT_RESULT)
        return self


class TrialRecord(StrictModel):
    schema_version: Literal["optimization-trial/v1"] = "optimization-trial/v1"
    trial_id: TrialId
    study_id: StudyId
    trial_number: int = Field(ge=0, le=1_000_000)
    candidate_id: CandidateId
    parameters: VllmTuningSpec
    status: TrialStatus
    objective: NumericMetricValue | None = None
    constraints: tuple[ConstraintEvaluation, ...] = Field(default=(), max_length=16)
    provenance: MeasuredProvenance | None = None
    evidence: tuple[ArtifactRef, ...] = Field(default=(), max_length=64)
    error: ErrorEnvelope | None = None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        measured_statuses = {TrialStatus.COMPLETED, TrialStatus.CONSTRAINT_FAILED}
        has_measured_data = (
            self.objective is not None or self.provenance is not None or bool(self.constraints)
        )
        if self.status in measured_statuses:
            if (
                self.objective is None
                or self.provenance is None
                or not self.constraints
                or not self.evidence
            ):
                raise ValueError(INCOMPLETE_TRIAL_RESULT)
            if self.error is not None:
                raise ValueError(INVALID_TRIAL_STATE_DATA)
        elif has_measured_data:
            raise ValueError(INVALID_TRIAL_STATE_DATA)
        if self.objective is not None and (
            self.objective.metric is not NumericMetric.SUCCESSFUL_OUTPUT_TOKENS_PER_SECOND
        ):
            raise ValueError(INVALID_OBJECTIVE_METRIC)
        failed_statuses = {
            TrialStatus.REJECTED_STATIC,
            TrialStatus.DEPLOYMENT_FAILED,
            TrialStatus.BENCHMARK_FAILED,
            TrialStatus.OOM,
        }
        if self.status in failed_statuses and self.error is None:
            raise ValueError(MISSING_TRIAL_ERROR)
        if self.status in {TrialStatus.SUGGESTED, TrialStatus.CANCELLED} and self.error is not None:
            raise ValueError(INVALID_TRIAL_STATE_DATA)
        if self.status in {TrialStatus.SUGGESTED, TrialStatus.REJECTED_STATIC} and self.evidence:
            raise ValueError(INVALID_TRIAL_STATE_DATA)
        if self.status is TrialStatus.COMPLETED and any(
            not constraint.passed for constraint in self.constraints
        ):
            raise ValueError(INVALID_CONSTRAINT_RESULT)
        if self.status is TrialStatus.CONSTRAINT_FAILED and (
            not self.constraints or all(constraint.passed for constraint in self.constraints)
        ):
            raise ValueError(INVALID_CONSTRAINT_RESULT)
        return self


class VerificationSummary(StrictModel):
    schema_version: Literal["verification-summary/v1"] = "verification-summary/v1"
    candidate_id: CandidateId
    objective: ObjectiveSpec
    repeat_values: tuple[PositiveMetric, ...] = Field(min_length=2, max_length=20)
    mean: PositiveMetric
    standard_deviation: float = Field(ge=0)
    coefficient_of_variation: float = Field(ge=0)
    worst_value: PositiveMetric
    constraints_satisfied: bool
    provenance: DerivedProvenance

    @model_validator(mode="after")
    def validate_statistics(self) -> Self:
        expected_mean = statistics.fmean(self.repeat_values)
        expected_standard_deviation = statistics.pstdev(self.repeat_values)
        expected_coefficient_of_variation = expected_standard_deviation / expected_mean
        expected_worst_value = min(self.repeat_values)
        supplied = (
            self.mean,
            self.standard_deviation,
            self.coefficient_of_variation,
            self.worst_value,
        )
        expected = (
            expected_mean,
            expected_standard_deviation,
            expected_coefficient_of_variation,
            expected_worst_value,
        )
        if not all(
            math.isclose(actual, calculated, rel_tol=1e-9, abs_tol=1e-9)
            for actual, calculated in zip(supplied, expected, strict=True)
        ):
            raise ValueError(INVALID_VERIFICATION_STATISTICS)
        return self


class ChampionSelection(StrictModel):
    schema_version: Literal["champion-selection/v1"] = "champion-selection/v1"
    champion_candidate_id: CandidateId
    fallback_candidate_id: CandidateId
    objective: ObjectiveSpec
    verified_candidates: tuple[VerificationSummary, ...] = Field(min_length=3, max_length=3)
    max_coefficient_of_variation: float = Field(gt=0, le=1)
    selection_artifact: ArtifactRef
    requires_human_approval: Literal[True] = True

    @model_validator(mode="after")
    def validate_candidates(self) -> Self:
        verified_ids = [summary.candidate_id for summary in self.verified_candidates]
        selected_ids = {self.champion_candidate_id, self.fallback_candidate_id}
        if (
            self.champion_candidate_id == self.fallback_candidate_id
            or not selected_ids.issubset(set(verified_ids))
            or len(verified_ids) != len(set(verified_ids))
            or any(not summary.constraints_satisfied for summary in self.verified_candidates)
            or any(summary.objective != self.objective for summary in self.verified_candidates)
        ):
            raise ValueError(INVALID_CHAMPION)
        if any(
            summary.coefficient_of_variation > self.max_coefficient_of_variation
            for summary in self.verified_candidates
        ):
            raise ValueError(UNSTABLE_CHAMPION)
        ranked = sorted(
            self.verified_candidates,
            key=lambda summary: (
                -summary.mean,
                -summary.worst_value,
                summary.coefficient_of_variation,
                str(summary.candidate_id),
            ),
        )
        if (
            self.champion_candidate_id != ranked[0].candidate_id
            or self.fallback_candidate_id != ranked[1].candidate_id
        ):
            raise ValueError(INVALID_CHAMPION)
        return self
