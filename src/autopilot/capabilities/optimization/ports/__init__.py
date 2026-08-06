"""Optimization Provider port implemented by the pinned Optuna adapter."""

from typing import Protocol

from autopilot.capabilities.optimization.domain.models import (
    CompiledOptunaStudy,
    OptimizationProviderTrial,
    OptimizationStudyRef,
    OptimizationSuggestion,
    OptimizationTrialOutcome,
    OptunaVersionProfile,
)
from autopilot.capabilities.optimization.ports.models import (
    OptimizationTrialCheckpoint,
    OptimizationTrialCompletion,
    OptimizationTrialDraft,
    OptimizationTrialEntry,
    OptimizationTrialKey,
)
from autopilot.domain.identifiers import ExperimentId, PlanHash, StudyId


class OptimizationStudyAdapter(Protocol):
    @property
    def profile(self) -> OptunaVersionProfile: ...

    def create_or_load(self, study: CompiledOptunaStudy) -> OptimizationStudyRef: ...

    def ask(
        self,
        study: CompiledOptunaStudy,
        reference: OptimizationStudyRef,
    ) -> OptimizationSuggestion: ...

    def tell(
        self,
        study: CompiledOptunaStudy,
        reference: OptimizationStudyRef,
        outcome: OptimizationTrialOutcome,
    ) -> OptimizationProviderTrial: ...

    def get_trials(
        self,
        study: CompiledOptunaStudy,
        reference: OptimizationStudyRef,
    ) -> tuple[OptimizationProviderTrial, ...]: ...


class OptimizationTrialRepository(Protocol):
    def add_suggested(self, draft: OptimizationTrialDraft) -> OptimizationTrialEntry: ...

    def mark_pending(
        self,
        key: OptimizationTrialKey,
        checkpoint: OptimizationTrialCheckpoint,
    ) -> OptimizationTrialEntry: ...

    def complete(
        self,
        key: OptimizationTrialKey,
        completion: OptimizationTrialCompletion,
    ) -> OptimizationTrialEntry: ...

    def get(self, key: OptimizationTrialKey) -> OptimizationTrialEntry | None: ...

    def list_for_study(
        self,
        *,
        experiment_id: ExperimentId,
        study_id: StudyId,
        plan_hash: PlanHash,
    ) -> tuple[OptimizationTrialEntry, ...]: ...
