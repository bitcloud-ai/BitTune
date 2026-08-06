"""Recoverable Top-candidate verification for the M7 fixed GPU workflow."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Literal, Protocol, Self

from pydantic import Field, model_validator

from autopilot.capabilities.evidence.application.champion import build_verification_summary
from autopilot.capabilities.evidence.domain.models import ChampionPolicy, EvidenceRunRef
from autopilot.capabilities.optimization.application.executor import (
    FixedTrialExecutor,
    TrialExecutionRequest,
)
from autopilot.capabilities.optimization.domain.enums import (
    TrialExecutionStage,
)
from autopilot.capabilities.optimization.domain.enums import (
    VerificationRunState as VerificationLifecycle,
)
from autopilot.capabilities.optimization.domain.errors import TrialExecutionPendingError
from autopilot.capabilities.optimization.ports.models import OptimizationTrialEntry
from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.base import StrictModel, UtcDatetime, utc_now
from autopilot.domain.candidates import DeploymentCandidate
from autopilot.domain.enums import TrialStatus
from autopilot.domain.identifiers import CandidateId, ExperimentId, PlanHash, PlanId, TrialId
from autopilot.domain.provenance import DerivedProvenance
from autopilot.domain.trials import TrialRecord, VerificationSummary

TOP_CANDIDATE_COUNT = 3
INVALID_CANDIDATES = "verification requires three distinct feasible source candidates"
INVALID_STATE = "verification state does not match its lifecycle"
INVALID_REQUEST = "verification request changed its immutable candidate binding"


class VerificationCandidate(StrictModel):
    """A feasible optimization Trial selected for repeated measurement."""

    candidate: DeploymentCandidate
    source_trial: TrialRecord

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if (
            self.source_trial.status is not TrialStatus.COMPLETED
            or self.source_trial.candidate_id != self.candidate.candidate_id
            or self.source_trial.objective is None
            or not self.source_trial.evidence
        ):
            raise ValueError(INVALID_CANDIDATES)
        return self


class VerificationRepeat(StrictModel):
    """One terminal verification measurement and its Tracking reference."""

    schema_version: Literal["verification-repeat/v1"] = "verification-repeat/v1"
    candidate_id: CandidateId
    repeat_index: int = Field(ge=0, le=19)
    trial: TrialRecord
    evidence_run: EvidenceRunRef

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        if (
            self.trial.candidate_id != self.candidate_id
            or self.trial.status is TrialStatus.SUGGESTED
            or self.evidence_run.trial_id != self.trial.trial_id
        ):
            raise ValueError(INVALID_REQUEST)
        return self


class VerificationCheckpoint(StrictModel):
    schema_version: Literal["verification-checkpoint/v1"] = "verification-checkpoint/v1"
    candidate_index: int = Field(ge=0, lt=TOP_CANDIDATE_COUNT)
    repeat_index: int = Field(ge=0, le=19)
    stage: TrialExecutionStage
    provider_resource_id: str = Field(min_length=1, max_length=256)


class VerificationRunState(StrictModel):
    """Small Job progress payload sufficient to resume verification after restart."""

    schema_version: Literal["verification-run-state/v1"] = "verification-run-state/v1"
    experiment_id: ExperimentId
    plan_id: PlanId
    plan_hash: PlanHash
    policy: ChampionPolicy
    candidate_ids: tuple[CandidateId, ...] = Field(
        min_length=TOP_CANDIDATE_COUNT,
        max_length=TOP_CANDIDATE_COUNT,
    )
    state: VerificationLifecycle
    candidate_index: int = Field(ge=0, le=TOP_CANDIDATE_COUNT)
    repeat_index: int = Field(ge=0, le=20)
    repeats: tuple[VerificationRepeat, ...] = Field(max_length=TOP_CANDIDATE_COUNT * 20)
    checkpoint: VerificationCheckpoint | None = None
    failure_reason: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if len(set(self.candidate_ids)) != TOP_CANDIDATE_COUNT:
            raise ValueError(INVALID_CANDIDATES)
        if self.state is VerificationLifecycle.PENDING and self.checkpoint is None:
            raise ValueError(INVALID_STATE)
        if self.state is not VerificationLifecycle.PENDING and self.checkpoint is not None:
            raise ValueError(INVALID_STATE)
        if self.state in {VerificationLifecycle.RUNNING, VerificationLifecycle.PENDING}:
            if self.failure_reason is not None or self.candidate_index >= TOP_CANDIDATE_COUNT:
                raise ValueError(INVALID_STATE)
        elif self.state is VerificationLifecycle.SUCCEEDED and len(self.repeats) != (
            TOP_CANDIDATE_COUNT * self.policy.verification_repeats
        ):
            raise ValueError(INVALID_STATE)
        return self


class VerificationAdvanceResult(StrictModel):
    schema_version: Literal["verification-advance-result/v1"] = "verification-advance-result/v1"
    state: VerificationRunState
    repeat: VerificationRepeat | None = None


class VerificationRequestFactory(Protocol):
    """Build a fixed, already-authorized request for one verification repeat."""

    def build_request(
        self,
        *,
        candidate: VerificationCandidate,
        trial_id: TrialId,
        repeat_index: int,
        started_at: UtcDatetime,
    ) -> TrialExecutionRequest: ...


def select_top_candidates(
    entries: tuple[OptimizationTrialEntry, ...],
    *,
    candidates: Mapping[CandidateId, DeploymentCandidate],
) -> tuple[VerificationCandidate, ...]:
    """Select only evidence-complete feasible Trials using a stable tie-breaker."""
    feasible_entries = [
        entry
        for entry in entries
        if entry.trial.status is TrialStatus.COMPLETED
        and entry.trial.objective is not None
        and entry.trial.evidence
        and entry.trial.candidate_id in candidates
    ]
    ranked = sorted(
        feasible_entries,
        key=lambda entry: (-_objective_value(entry), str(entry.trial.candidate_id)),
    )
    if len(ranked) < TOP_CANDIDATE_COUNT:
        raise ValueError(INVALID_CANDIDATES)
    return tuple(
        VerificationCandidate(
            candidate=candidates[entry.trial.candidate_id],
            source_trial=entry.trial,
        )
        for entry in ranked[:TOP_CANDIDATE_COUNT]
    )


class VerificationController:
    """Advance exactly one repeat and persist only structured Job progress."""

    def __init__(
        self,
        *,
        trial_executor: FixedTrialExecutor,
        request_factory: VerificationRequestFactory,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._trial_executor = trial_executor
        self._request_factory = request_factory
        self._clock = clock

    @staticmethod
    def start(
        *,
        experiment_id: ExperimentId,
        plan_id: PlanId,
        plan_hash: PlanHash,
        candidates: tuple[VerificationCandidate, ...],
        policy: ChampionPolicy,
    ) -> VerificationRunState:
        if len(candidates) != policy.top_candidate_count:
            raise ValueError(INVALID_CANDIDATES)
        return VerificationRunState(
            experiment_id=experiment_id,
            plan_id=plan_id,
            plan_hash=plan_hash,
            policy=policy,
            candidate_ids=tuple(item.candidate.candidate_id for item in candidates),
            state=VerificationLifecycle.RUNNING,
            candidate_index=0,
            repeat_index=0,
            repeats=(),
        )

    def advance(
        self,
        state: VerificationRunState,
        *,
        candidates: tuple[VerificationCandidate, ...],
        started_at: UtcDatetime,
        cancellation_requested: Callable[[], bool] = lambda: False,
    ) -> VerificationAdvanceResult:
        if state.state in {
            VerificationLifecycle.SUCCEEDED,
            VerificationLifecycle.FAILED,
            VerificationLifecycle.CANCELLED,
        }:
            return VerificationAdvanceResult(state=state)
        if tuple(item.candidate.candidate_id for item in candidates) != state.candidate_ids:
            raise ValueError(INVALID_CANDIDATES)
        candidate = candidates[state.candidate_index]
        trial_id = derive_verification_trial_id(candidate.source_trial.trial_id, state.repeat_index)
        request = self._request_factory.build_request(
            candidate=candidate,
            trial_id=trial_id,
            repeat_index=state.repeat_index,
            started_at=started_at,
        )
        if request.trial_id != trial_id or (
            request.candidate.candidate_id != candidate.candidate.candidate_id
        ):
            raise ValueError(INVALID_REQUEST)
        try:
            result = self._trial_executor.execute(
                request,
                cancellation_requested=cancellation_requested,
                active_stage=state.checkpoint.stage if state.checkpoint is not None else None,
            )
        except TrialExecutionPendingError as pending:
            next_state = state.model_validate(
                {
                    **state.model_dump(mode="python"),
                    "state": VerificationLifecycle.PENDING,
                    "checkpoint": VerificationCheckpoint(
                        candidate_index=state.candidate_index,
                        repeat_index=state.repeat_index,
                        stage=pending.stage,
                        provider_resource_id=pending.provider_resource_id,
                    ),
                }
            )
            return VerificationAdvanceResult(state=next_state)
        repeat = VerificationRepeat(
            candidate_id=candidate.candidate.candidate_id,
            repeat_index=state.repeat_index,
            trial=result.trial,
            evidence_run=result.evidence_run,
        )
        repeats = (*state.repeats, repeat)
        cancelled = result.trial.status is TrialStatus.CANCELLED
        next_candidate_index = state.candidate_index
        next_repeat_index = state.repeat_index + 1
        if next_repeat_index >= state.policy.verification_repeats:
            next_candidate_index += 1
            next_repeat_index = 0
        terminal_state = (
            VerificationLifecycle.CANCELLED
            if cancelled
            else (
                VerificationLifecycle.SUCCEEDED
                if next_candidate_index >= TOP_CANDIDATE_COUNT
                else VerificationLifecycle.RUNNING
            )
        )
        next_state = state.model_validate(
            {
                **state.model_dump(mode="python"),
                "state": terminal_state,
                "candidate_index": next_candidate_index,
                "repeat_index": next_repeat_index,
                "repeats": repeats,
                "checkpoint": None,
            }
        )
        return VerificationAdvanceResult(state=next_state, repeat=repeat)


def derive_verification_trial_id(source_trial_id: TrialId, repeat_index: int) -> TrialId:
    digest = hashlib.sha256(f"verification:{source_trial_id}:{repeat_index}".encode()).hexdigest()[
        :32
    ]
    return TrialId(root=f"trial_{digest}")


def _objective_value(entry: OptimizationTrialEntry) -> float:
    if entry.trial.objective is None:
        raise ValueError(INVALID_CANDIDATES)
    return entry.trial.objective.value


def build_verification_summaries(
    state: VerificationRunState,
    candidates: tuple[VerificationCandidate, ...],
    *,
    calculation_artifact: ArtifactRef,
) -> tuple[VerificationSummary, ...]:
    """Derive summaries only from measured, constraint-satisfying repeats."""
    if state.state is not VerificationLifecycle.SUCCEEDED:
        raise ValueError(INVALID_STATE)
    summaries: list[VerificationSummary] = []
    for candidate in candidates:
        repeats = tuple(
            item for item in state.repeats if item.candidate_id == candidate.candidate.candidate_id
        )
        if len(repeats) != state.policy.verification_repeats or any(
            item.trial.status not in {TrialStatus.COMPLETED, TrialStatus.CONSTRAINT_FAILED}
            or item.trial.objective is None
            for item in repeats
        ):
            continue
        values = tuple(
            objective.value for item in repeats if (objective := item.trial.objective) is not None
        )
        summaries.append(
            build_verification_summary(
                candidate.candidate.candidate_id,
                values,
                tuple(
                    item.trial.status is TrialStatus.COMPLETED
                    and all(constraint.passed for constraint in item.trial.constraints)
                    for item in repeats
                ),
                DerivedProvenance(
                    provider="autopilot-verification",
                    provider_version="m7",
                    adapter_version="verification/v1",
                    calculation_artifact=calculation_artifact,
                    input_artifacts=tuple(
                        artifact for item in repeats for artifact in item.trial.evidence
                    ),
                ),
                state.policy,
            )
        )
    return tuple(summaries)


__all__ = [
    "VerificationAdvanceResult",
    "VerificationCandidate",
    "VerificationCheckpoint",
    "VerificationController",
    "VerificationRepeat",
    "VerificationRequestFactory",
    "VerificationRunState",
    "build_verification_summaries",
    "derive_verification_trial_id",
    "select_top_candidates",
]
