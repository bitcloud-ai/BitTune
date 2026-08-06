"""Deterministic one-Trial-at-a-time Optimization Controller."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, Self

from pydantic import Field, model_validator

from autopilot.capabilities.benchmark.domain.models import BenchmarkBudgetEstimate
from autopilot.capabilities.optimization.application.compiler import compile_optuna_study
from autopilot.capabilities.optimization.application.executor import (
    FixedTrialExecutor,
    TrialExecutionRequest,
    TrialExecutionResult,
)
from autopilot.capabilities.optimization.domain.enums import (
    OptimizationProviderTrialState,
    OptimizationRunState,
    OptimizationStopReason,
)
from autopilot.capabilities.optimization.domain.errors import (
    OptimizationTrialConflictError,
    TrialExecutionPendingError,
)
from autopilot.capabilities.optimization.domain.models import (
    CompiledOptunaStudy,
    OptimizationExecutionSpecification,
    OptimizationProviderTrial,
    OptimizationStudyRef,
    OptimizationSuggestion,
    OptimizationTrialOutcome,
)
from autopilot.capabilities.optimization.ports import (
    OptimizationStudyAdapter,
    OptimizationTrialRepository,
)
from autopilot.capabilities.optimization.ports.models import (
    OptimizationTrialCheckpoint,
    OptimizationTrialCompletion,
    OptimizationTrialDraft,
    OptimizationTrialEntry,
    TrialBudgetReservation,
)
from autopilot.domain.base import StrictModel, UtcDatetime, utc_now
from autopilot.domain.candidates import VllmTuningSpec
from autopilot.domain.enums import PlanKind, PlanStatus, TrialStatus
from autopilot.domain.hashing import compute_content_hash
from autopilot.domain.identifiers import CandidateId, PlanHash, StudyId, TrialId
from autopilot.domain.plans import PlanEnvelope
from autopilot.domain.trials import TrialRecord

INVALID_RUN_PLAN = "Optimization Controller requires an approved Optimization Plan"
INVALID_RUN_START = "Optimization run cannot start before its immutable Plan"
INVALID_PROFILE_BINDING = "Optimization Plan Provider profile does not match Optuna"
INVALID_TRIAL_REQUEST = "Trial Request Factory returned material different from the suggestion"
INVALID_RESERVATION = "Trial Request budget estimate changed after reservation"
MISSING_PROVIDER_TRIAL = "persisted Optimization Trial has no matching Optuna Trial"
MULTIPLE_ACTIVE_TRIALS = "more than one Optimization Trial is active for one Study"
ORPHAN_TERMINAL_TRIAL = "Optuna has a terminal outcome without application evidence"
STOPPED_REQUIRES_REASON = "stopped Optimization run requires a stop reason"
RUNNING_HAS_REASON = "running Optimization run cannot have a stop reason"
INVALID_PROGRESS_COUNTERS = "Optimization progress counters are inconsistent"


class OptimizationRunRequest(StrictModel):
    schema_version: Literal["optimization-run-request/v1"] = "optimization-run-request/v1"
    plan: PlanEnvelope[OptimizationExecutionSpecification]
    started_at: UtcDatetime

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        if (
            self.plan.kind is not PlanKind.OPTIMIZATION
            or self.plan.status is not PlanStatus.APPROVED
        ):
            raise ValueError(INVALID_RUN_PLAN)
        if self.started_at < self.plan.created_at:
            raise ValueError(INVALID_RUN_START)
        return self


class OptimizationProgress(StrictModel):
    schema_version: Literal["optimization-progress/v1"] = "optimization-progress/v1"
    state: OptimizationRunState
    study_id: StudyId
    trial_count: int = Field(ge=0)
    completed_count: int = Field(ge=0)
    feasible_count: int = Field(ge=0)
    reserved_requests: int = Field(ge=0)
    reserved_input_tokens: int = Field(ge=0)
    reserved_output_tokens: int = Field(ge=0)
    active_trial_id: TrialId | None = None
    stop_reason: OptimizationStopReason | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.state is OptimizationRunState.STOPPED and self.stop_reason is None:
            raise ValueError(STOPPED_REQUIRES_REASON)
        if self.state is not OptimizationRunState.STOPPED and self.stop_reason is not None:
            raise ValueError(RUNNING_HAS_REASON)
        if self.completed_count > self.trial_count or self.feasible_count > self.completed_count:
            raise ValueError(INVALID_PROGRESS_COUNTERS)
        return self


class OptimizationAdvanceResult(StrictModel):
    schema_version: Literal["optimization-advance-result/v1"] = "optimization-advance-result/v1"
    progress: OptimizationProgress
    terminal_trial: OptimizationTrialEntry | None = None


class OptimizationTrialRequestFactory(Protocol):
    """Build a fully authorized fixed Trial request for one Optuna suggestion."""

    def estimate_reservation(
        self,
        specification: OptimizationExecutionSpecification,
    ) -> TrialBudgetReservation: ...

    def build_request(
        self,
        *,
        plan: PlanEnvelope[OptimizationExecutionSpecification],
        suggestion: OptimizationSuggestion,
        trial_id: TrialId,
        candidate_id: CandidateId,
        started_at: UtcDatetime,
    ) -> TrialExecutionRequest: ...


@dataclass(frozen=True, slots=True)
class _ExecutionContext:
    request: OptimizationRunRequest
    compiled: CompiledOptunaStudy
    reference: OptimizationStudyRef
    provider_trials: tuple[OptimizationProviderTrial, ...]
    cancellation_requested: Callable[[], bool]


class OptimizationController:
    """Advance one persisted Trial and reconcile provider state on every call."""

    def __init__(
        self,
        *,
        study_adapter: OptimizationStudyAdapter,
        trial_repository: OptimizationTrialRepository,
        trial_executor: FixedTrialExecutor,
        request_factory: OptimizationTrialRequestFactory,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._study_adapter = study_adapter
        self._trial_repository = trial_repository
        self._trial_executor = trial_executor
        self._request_factory = request_factory
        self._clock = clock

    def advance(
        self,
        request: OptimizationRunRequest,
        *,
        cancellation_requested: Callable[[], bool] = lambda: False,
    ) -> OptimizationAdvanceResult:
        """Reconcile then execute at most one Trial or return a deterministic stop."""
        specification = request.plan.execution_specification
        self._validate_profile(specification)
        compiled = compile_optuna_study(
            specification.definition,
            request.plan.plan_hash,
            self._study_adapter.profile,
        )
        reference = self._study_adapter.create_or_load(compiled)
        entries = self._entries(request)
        provider_trials = self._study_adapter.get_trials(compiled, reference)
        self._reconcile_terminal_entries(compiled, reference, entries)
        provider_trials = self._study_adapter.get_trials(compiled, reference)

        active = [entry for entry in entries if entry.trial.status is TrialStatus.SUGGESTED]
        if len(active) > 1:
            raise OptimizationTrialConflictError(MULTIPLE_ACTIVE_TRIALS)
        if active:
            return self._execute_entry(
                _ExecutionContext(
                    request,
                    compiled,
                    reference,
                    provider_trials,
                    cancellation_requested,
                ),
                active[0],
            )

        orphan = self._find_orphan_running(provider_trials, entries)
        if orphan is not None:
            entry = self._create_entry_from_suggestion(request, orphan)
            return self._execute_entry(
                _ExecutionContext(
                    request,
                    compiled,
                    reference,
                    provider_trials,
                    cancellation_requested,
                ),
                entry,
            )

        if cancellation_requested():
            return self._stopped(request, entries, OptimizationStopReason.CANCELLED)
        stop_reason = self._stop_reason(request, entries, len(compiled.configurations))
        if stop_reason is not None:
            return self._stopped(request, entries, stop_reason)
        reservation = self._request_factory.estimate_reservation(specification)
        stop_reason = self._budget_stop_reason(request, entries, reservation)
        if stop_reason is not None:
            return self._stopped(request, entries, stop_reason)

        suggestion = self._study_adapter.ask(compiled, reference)
        entry = self._create_entry_from_suggestion(request, suggestion)
        provider_trials = self._study_adapter.get_trials(compiled, reference)
        return self._execute_entry(
            _ExecutionContext(
                request,
                compiled,
                reference,
                provider_trials,
                cancellation_requested,
            ),
            entry,
        )

    def _create_entry_from_suggestion(
        self,
        request: OptimizationRunRequest,
        suggestion: OptimizationSuggestion,
    ) -> OptimizationTrialEntry:
        trial_id = derive_trial_id(suggestion.study_id, suggestion.trial_number)
        candidate_id = derive_candidate_id(request.plan.plan_hash, suggestion.parameters)
        trial_request = self._request_factory.build_request(
            plan=request.plan,
            suggestion=suggestion,
            trial_id=trial_id,
            candidate_id=candidate_id,
            started_at=request.started_at,
        )
        _validate_trial_request(
            trial_request,
            request,
            suggestion,
            trial_id,
            candidate_id,
        )
        reservation = reservation_from_benchmark(trial_request.benchmark.budget_estimate)
        expected = self._request_factory.estimate_reservation(request.plan.execution_specification)
        if reservation != expected:
            raise OptimizationTrialConflictError(INVALID_RESERVATION)
        draft = OptimizationTrialDraft(
            experiment_id=request.plan.experiment_id,
            plan_id=request.plan.plan_id,
            plan_hash=request.plan.plan_hash,
            trial=TrialRecord(
                trial_id=trial_id,
                study_id=suggestion.study_id,
                trial_number=suggestion.trial_number,
                candidate_id=candidate_id,
                parameters=suggestion.parameters,
                status=TrialStatus.SUGGESTED,
            ),
            benchmark_run_id=trial_request.benchmark_context.benchmark_run_id,
            reservation=reservation,
        )
        return self._trial_repository.add_suggested(draft)

    def _execute_entry(
        self,
        context: _ExecutionContext,
        entry: OptimizationTrialEntry,
    ) -> OptimizationAdvanceResult:
        provider_trial = next(
            (
                item
                for item in context.provider_trials
                if item.suggestion.trial_number == entry.trial.trial_number
            ),
            None,
        )
        if provider_trial is None:
            raise OptimizationTrialConflictError(MISSING_PROVIDER_TRIAL)
        if (
            provider_trial.state is not OptimizationProviderTrialState.RUNNING
            or provider_trial.domain_status is not None
            or provider_trial.suggestion.study_id != entry.trial.study_id
            or provider_trial.suggestion.parameters != entry.trial.parameters
        ):
            raise OptimizationTrialConflictError(ORPHAN_TERMINAL_TRIAL)
        suggestion = provider_trial.suggestion
        trial_request = self._request_factory.build_request(
            plan=context.request.plan,
            suggestion=suggestion,
            trial_id=entry.trial.trial_id,
            candidate_id=entry.trial.candidate_id,
            started_at=context.request.started_at,
        )
        _validate_trial_request(
            trial_request,
            context.request,
            suggestion,
            entry.trial.trial_id,
            entry.trial.candidate_id,
        )
        if (
            trial_request.benchmark_context.benchmark_run_id != entry.benchmark_run_id
            or reservation_from_benchmark(trial_request.benchmark.budget_estimate)
            != entry.reservation
        ):
            raise OptimizationTrialConflictError(INVALID_RESERVATION)
        try:
            result = self._trial_executor.execute(
                trial_request,
                cancellation_requested=context.cancellation_requested,
                active_stage=entry.checkpoint.stage if entry.checkpoint is not None else None,
            )
        except TrialExecutionPendingError as pending:
            pending_entry = self._trial_repository.mark_pending(
                entry.key(),
                OptimizationTrialCheckpoint(
                    stage=pending.stage,
                    provider_resource_id=pending.provider_resource_id,
                ),
            )
            return OptimizationAdvanceResult(
                progress=self._progress(
                    context.request,
                    self._entries(context.request),
                    state=OptimizationRunState.PENDING,
                    active_trial_id=pending_entry.trial.trial_id,
                )
            )

        completed = self._trial_repository.complete(
            entry.key(),
            OptimizationTrialCompletion(
                trial=result.trial,
                evidence_run=result.evidence_run,
            ),
        )
        self._study_adapter.tell(
            context.compiled,
            context.reference,
            outcome_for_trial(result),
        )
        entries = self._entries(context.request)
        stop_reason = (
            OptimizationStopReason.CANCELLED
            if context.cancellation_requested()
            else self._stop_reason(
                context.request,
                entries,
                len(context.compiled.configurations),
            )
        )
        progress = self._progress(
            context.request,
            entries,
            state=(
                OptimizationRunState.STOPPED
                if stop_reason is not None
                else OptimizationRunState.RUNNING
            ),
            stop_reason=stop_reason,
        )
        return OptimizationAdvanceResult(progress=progress, terminal_trial=completed)

    def _reconcile_terminal_entries(
        self,
        compiled: CompiledOptunaStudy,
        reference: OptimizationStudyRef,
        entries: list[OptimizationTrialEntry],
    ) -> None:
        for entry in entries:
            if entry.trial.status is not TrialStatus.SUGGESTED:
                self._study_adapter.tell(
                    compiled,
                    reference,
                    outcome_for_record(entry.trial),
                )

    @staticmethod
    def _find_orphan_running(
        provider_trials: tuple[OptimizationProviderTrial, ...],
        entries: list[OptimizationTrialEntry],
    ) -> OptimizationSuggestion | None:
        known_numbers = {entry.trial.trial_number for entry in entries}
        unknown = [
            item for item in provider_trials if item.suggestion.trial_number not in known_numbers
        ]
        if any(
            item.state is not OptimizationProviderTrialState.RUNNING
            or item.domain_status is not None
            for item in unknown
        ):
            raise OptimizationTrialConflictError(ORPHAN_TERMINAL_TRIAL)
        if len(unknown) > 1:
            raise OptimizationTrialConflictError(MULTIPLE_ACTIVE_TRIALS)
        return unknown[0].suggestion if unknown else None

    def _entries(self, request: OptimizationRunRequest) -> list[OptimizationTrialEntry]:
        specification = request.plan.execution_specification
        return list(
            self._trial_repository.list_for_study(
                experiment_id=request.plan.experiment_id,
                study_id=specification.definition.study_id,
                plan_hash=request.plan.plan_hash,
            )
        )

    def _validate_profile(self, specification: OptimizationExecutionSpecification) -> None:
        profile = self._study_adapter.profile
        if (
            specification.provider_version != profile.provider_version
            or specification.adapter_version != profile.adapter_version
            or specification.provider_profile_version != profile.profile_version
        ):
            raise OptimizationTrialConflictError(INVALID_PROFILE_BINDING)

    def _stop_reason(
        self,
        request: OptimizationRunRequest,
        entries: list[OptimizationTrialEntry],
        configuration_count: int,
    ) -> OptimizationStopReason | None:
        policy = request.plan.execution_specification.convergence
        if len(entries) >= policy.maximum_trials:
            return OptimizationStopReason.TRIAL_BUDGET
        elapsed = (self._clock() - request.started_at).total_seconds()
        if elapsed >= request.plan.execution_specification.budget.max_duration_seconds:
            return OptimizationStopReason.WALL_CLOCK_BUDGET
        configurations = {entry.trial.parameters.model_dump_json() for entry in entries}
        if len(configurations) >= configuration_count:
            return OptimizationStopReason.SEARCH_SPACE_EXHAUSTED
        feasible = [
            entry.trial.objective.value
            for entry in entries
            if entry.trial.status is TrialStatus.COMPLETED and entry.trial.objective is not None
        ]
        if len(entries) >= policy.minimum_trials and len(feasible) > policy.no_improvement_trials:
            baseline = max(feasible[: -policy.no_improvement_trials])
            recent = max(feasible[-policy.no_improvement_trials :])
            relative = (recent - baseline) / baseline if baseline else 0
            if relative < policy.minimum_relative_improvement:
                return OptimizationStopReason.NO_IMPROVEMENT
        return None

    @staticmethod
    def _budget_stop_reason(
        request: OptimizationRunRequest,
        entries: list[OptimizationTrialEntry],
        reservation: TrialBudgetReservation,
    ) -> OptimizationStopReason | None:
        budget = request.plan.execution_specification.budget
        used_requests, used_input, used_output = _reserved_totals(entries)
        if used_requests + reservation.requests > budget.max_requests:
            return OptimizationStopReason.REQUEST_BUDGET
        if (
            used_input + reservation.input_tokens > budget.max_input_tokens
            or used_output + reservation.output_tokens > budget.max_output_tokens
        ):
            return OptimizationStopReason.TOKEN_BUDGET
        return None

    def _stopped(
        self,
        request: OptimizationRunRequest,
        entries: list[OptimizationTrialEntry],
        reason: OptimizationStopReason,
    ) -> OptimizationAdvanceResult:
        return OptimizationAdvanceResult(
            progress=self._progress(
                request,
                entries,
                state=OptimizationRunState.STOPPED,
                stop_reason=reason,
            )
        )

    @staticmethod
    def _progress(
        request: OptimizationRunRequest,
        entries: list[OptimizationTrialEntry],
        *,
        state: OptimizationRunState,
        active_trial_id: TrialId | None = None,
        stop_reason: OptimizationStopReason | None = None,
    ) -> OptimizationProgress:
        reserved_requests, reserved_input, reserved_output = _reserved_totals(entries)
        completed = [entry for entry in entries if entry.trial.status is not TrialStatus.SUGGESTED]
        feasible = [entry for entry in completed if entry.trial.status is TrialStatus.COMPLETED]
        return OptimizationProgress(
            state=state,
            study_id=request.plan.execution_specification.definition.study_id,
            trial_count=len(entries),
            completed_count=len(completed),
            feasible_count=len(feasible),
            reserved_requests=reserved_requests,
            reserved_input_tokens=reserved_input,
            reserved_output_tokens=reserved_output,
            active_trial_id=active_trial_id,
            stop_reason=stop_reason,
        )


def reservation_from_benchmark(estimate: BenchmarkBudgetEstimate) -> TrialBudgetReservation:
    return TrialBudgetReservation(
        requests=estimate.total_requests,
        duration_seconds=estimate.estimated_duration_seconds,
        input_tokens=estimate.estimated_input_tokens,
        output_tokens=estimate.estimated_output_tokens,
    )


def outcome_for_trial(result: TrialExecutionResult) -> OptimizationTrialOutcome:
    return outcome_for_record(result.trial)


def outcome_for_record(trial: TrialRecord) -> OptimizationTrialOutcome:
    return OptimizationTrialOutcome(
        trial_number=trial.trial_number,
        status=trial.status,
        objective_value=(
            trial.objective.value
            if trial.status is TrialStatus.COMPLETED and trial.objective is not None
            else None
        ),
    )


def derive_trial_id(study_id: StudyId, trial_number: int) -> TrialId:
    digest = hashlib.sha256(f"trial:{study_id}:{trial_number}".encode()).hexdigest()[:32]
    return TrialId(root=f"trial_{digest}")


def derive_candidate_id(plan_hash: PlanHash, parameters: VllmTuningSpec) -> CandidateId:
    parameter_hash = compute_content_hash(parameters)
    digest = hashlib.sha256(f"candidate:{plan_hash}:{parameter_hash}".encode()).hexdigest()[:32]
    return CandidateId(root=f"cand_{digest}")


def _reserved_totals(entries: list[OptimizationTrialEntry]) -> tuple[int, int, int]:
    return (
        sum(entry.reservation.requests for entry in entries),
        sum(entry.reservation.input_tokens for entry in entries),
        sum(entry.reservation.output_tokens for entry in entries),
    )


def _validate_trial_request(
    request: TrialExecutionRequest,
    run_request: OptimizationRunRequest,
    suggestion: OptimizationSuggestion,
    trial_id: TrialId,
    candidate_id: CandidateId,
) -> None:
    specification = run_request.plan.execution_specification
    if (
        request.experiment_id != run_request.plan.experiment_id
        or request.study_id != suggestion.study_id
        or request.trial_id != trial_id
        or request.trial_number != suggestion.trial_number
        or request.base_candidate != specification.base_candidate
        or request.candidate.candidate_id != candidate_id
        or request.candidate.parameters != suggestion.parameters
        or request.search_space != specification.definition.search_space
    ):
        raise OptimizationTrialConflictError(INVALID_TRIAL_REQUEST)


__all__ = [
    "OptimizationAdvanceResult",
    "OptimizationController",
    "OptimizationProgress",
    "OptimizationRunRequest",
    "OptimizationTrialRequestFactory",
    "derive_candidate_id",
    "derive_trial_id",
    "reservation_from_benchmark",
]
