"""In-memory Trial ledger fake for deterministic Controller tests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from autopilot.capabilities.optimization.domain.errors import (
    OptimizationTrialConflictError,
    OptimizationTrialNotFoundError,
)
from autopilot.capabilities.optimization.ports.models import (
    OptimizationTrialCheckpoint,
    OptimizationTrialCompletion,
    OptimizationTrialDraft,
    OptimizationTrialEntry,
    OptimizationTrialKey,
)
from autopilot.domain.base import utc_now
from autopilot.domain.enums import TrialStatus
from autopilot.domain.identifiers import ExperimentId, PlanHash, StudyId

TRIAL_BINDING_CONFLICT = "Trial key is bound to different material"
PENDING_STATE_CONFLICT = "only suggested Trials can become pending"
TERMINAL_STATE_CONFLICT = "terminal Trial material is immutable"
COMPLETION_BINDING_CONFLICT = "Trial completion changed immutable material"
TRIAL_NOT_FOUND = "Optimization Trial does not exist"


class FakeOptimizationTrialRepository:
    """Enforce the same suggested-to-terminal transitions without PostgreSQL."""

    def __init__(self, clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock
        self._entries: dict[tuple[str, str, int], OptimizationTrialEntry] = {}

    def add_suggested(self, draft: OptimizationTrialDraft) -> OptimizationTrialEntry:
        key = self._storage_key(draft.key())
        existing = self._entries.get(key)
        now = self._clock()
        entry = OptimizationTrialEntry(
            experiment_id=draft.experiment_id,
            plan_id=draft.plan_id,
            plan_hash=draft.plan_hash,
            trial=draft.trial,
            benchmark_run_id=draft.benchmark_run_id,
            reservation=draft.reservation,
            created_at=now,
            updated_at=now,
            version=1,
        )
        if existing is not None:
            if self._same_material(existing, entry):
                return existing
            raise OptimizationTrialConflictError(TRIAL_BINDING_CONFLICT)
        self._entries[key] = entry
        return entry

    def mark_pending(
        self,
        key: OptimizationTrialKey,
        checkpoint: OptimizationTrialCheckpoint,
    ) -> OptimizationTrialEntry:
        existing = self._require(key)
        if existing.trial.status is not TrialStatus.SUGGESTED:
            raise OptimizationTrialConflictError(PENDING_STATE_CONFLICT)
        if existing.checkpoint == checkpoint:
            return existing
        updated = OptimizationTrialEntry.model_validate(
            {
                **existing.model_dump(mode="python"),
                "checkpoint": checkpoint,
                "updated_at": self._clock(),
                "version": existing.version + 1,
            }
        )
        self._entries[self._storage_key(key)] = updated
        return updated

    def complete(
        self,
        key: OptimizationTrialKey,
        completion: OptimizationTrialCompletion,
    ) -> OptimizationTrialEntry:
        existing = self._require(key)
        if existing.trial.status.value != "suggested":
            if (
                existing.trial == completion.trial
                and existing.evidence_run == completion.evidence_run
            ):
                return existing
            raise OptimizationTrialConflictError(TERMINAL_STATE_CONFLICT)
        if (
            completion.trial.trial_id != existing.trial.trial_id
            or completion.trial.study_id != existing.trial.study_id
            or completion.trial.trial_number != existing.trial.trial_number
            or completion.trial.candidate_id != existing.trial.candidate_id
            or completion.trial.parameters != existing.trial.parameters
        ):
            raise OptimizationTrialConflictError(COMPLETION_BINDING_CONFLICT)
        now = self._clock()
        updated = OptimizationTrialEntry.model_validate(
            {
                **existing.model_dump(mode="python"),
                "trial": completion.trial,
                "checkpoint": None,
                "evidence_run": completion.evidence_run,
                "updated_at": now,
                "ended_at": now,
                "version": existing.version + 1,
            }
        )
        self._entries[self._storage_key(key)] = updated
        return updated

    def get(self, key: OptimizationTrialKey) -> OptimizationTrialEntry | None:
        return self._entries.get(self._storage_key(key))

    def list_for_study(
        self,
        *,
        experiment_id: ExperimentId,
        study_id: StudyId,
        plan_hash: PlanHash,
    ) -> tuple[OptimizationTrialEntry, ...]:
        entries = (
            entry
            for entry in self._entries.values()
            if entry.experiment_id == experiment_id
            and entry.trial.study_id == study_id
            and entry.plan_hash == plan_hash
        )
        return tuple(sorted(entries, key=lambda item: item.trial.trial_number))

    def _require(self, key: OptimizationTrialKey) -> OptimizationTrialEntry:
        entry = self.get(key)
        if entry is None:
            raise OptimizationTrialNotFoundError(TRIAL_NOT_FOUND)
        return entry

    @staticmethod
    def _storage_key(key: OptimizationTrialKey) -> tuple[str, str, int]:
        return str(key.experiment_id), str(key.study_id), key.trial_number

    @staticmethod
    def _same_material(left: OptimizationTrialEntry, right: OptimizationTrialEntry) -> bool:
        excluded = {"created_at", "updated_at", "version"}
        return left.model_dump(exclude=excluded) == right.model_dump(exclude=excluded)


__all__ = ["FakeOptimizationTrialRepository"]
