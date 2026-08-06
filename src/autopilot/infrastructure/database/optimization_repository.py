"""PostgreSQL repository for recoverable Optimization Trial execution."""

from __future__ import annotations

from datetime import datetime
from typing import Final, cast

from pydantic import JsonValue, ValidationError
from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from autopilot.capabilities.evidence.domain.models import EvidenceRunRef
from autopilot.capabilities.optimization.domain.enums import TrialExecutionStage
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
    TrialBudgetReservation,
)
from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.candidates import VllmTuningSpec
from autopilot.domain.enums import PlanKind, PlanStatus, TrialStatus
from autopilot.domain.errors import ErrorEnvelope
from autopilot.domain.identifiers import (
    BenchmarkRunId,
    CandidateId,
    ExperimentId,
    PlanHash,
    PlanId,
    StudyId,
    TrialId,
)
from autopilot.domain.provenance import MeasuredProvenance
from autopilot.domain.trials import ConstraintEvaluation, NumericMetricValue, TrialRecord
from autopilot.infrastructure.database.models import OptimizationTrialRow, PlanRow

DATABASE_TIME_INVALID: Final = "PostgreSQL did not return an aware database timestamp"
TRIAL_NOT_FOUND: Final = "Optimization Trial does not exist"
TRIAL_CONFLICT: Final = "Optimization Trial is bound to different immutable material"
TRIAL_STATE_CONFLICT: Final = "Optimization Trial cannot make the requested transition"
TRIAL_DATA_INVALID: Final = "persisted Optimization Trial data is invalid"
PLAN_BINDING_INVALID: Final = "Optimization Trial requires its approved Optimization Plan"


def _database_now(session: Session) -> datetime:
    value: object = session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise OptimizationTrialConflictError(DATABASE_TIME_INVALID)
    return value


def _json_object(model: object) -> dict[str, JsonValue]:
    dumped = model.model_dump(mode="json")  # type: ignore[attr-defined]
    return cast(dict[str, JsonValue], dumped)


def _trial_from_row(row: OptimizationTrialRow) -> TrialRecord:
    objective = (
        NumericMetricValue.model_validate(row.objective_json)
        if row.objective_json is not None
        else None
    )
    provenance = (
        MeasuredProvenance.model_validate(row.provenance_json)
        if row.provenance_json is not None
        else None
    )
    error = ErrorEnvelope.model_validate(row.error_json) if row.error_json is not None else None
    return TrialRecord(
        schema_version=row.trial_schema_version,
        trial_id=TrialId(root=row.trial_id),
        study_id=StudyId(root=row.study_id),
        trial_number=row.trial_number,
        candidate_id=CandidateId(root=row.candidate_id),
        parameters=VllmTuningSpec.model_validate(row.parameters_json),
        status=TrialStatus(row.status),
        objective=objective,
        constraints=tuple(
            ConstraintEvaluation.model_validate(item) for item in row.constraints_json
        ),
        provenance=provenance,
        evidence=tuple(ArtifactRef.model_validate(item) for item in row.evidence_json),
        error=error,
    )


def _entry_from_row(row: OptimizationTrialRow) -> OptimizationTrialEntry:
    try:
        checkpoint = None
        if row.checkpoint_stage is not None and row.provider_resource_id is not None:
            checkpoint = OptimizationTrialCheckpoint(
                stage=TrialExecutionStage(row.checkpoint_stage),
                provider_resource_id=row.provider_resource_id,
            )
        evidence_run = (
            EvidenceRunRef.model_validate(row.evidence_run_json)
            if row.evidence_run_json is not None
            else None
        )
        return OptimizationTrialEntry(
            schema_version=row.schema_version,
            experiment_id=ExperimentId(root=row.experiment_id),
            plan_id=PlanId(root=row.plan_id),
            plan_hash=PlanHash(root=row.plan_hash),
            trial=_trial_from_row(row),
            benchmark_run_id=BenchmarkRunId(root=row.benchmark_run_id),
            reservation=TrialBudgetReservation.model_validate(row.reservation_json),
            checkpoint=checkpoint,
            evidence_run=evidence_run,
            created_at=row.created_at,
            updated_at=row.updated_at,
            ended_at=row.ended_at,
            version=row.version,
        )
    except (ValidationError, ValueError) as error:
        raise OptimizationTrialConflictError(TRIAL_DATA_INVALID) from error


def _key_statement(key: OptimizationTrialKey) -> Select[tuple[OptimizationTrialRow]]:
    return select(OptimizationTrialRow).where(
        OptimizationTrialRow.experiment_id == str(key.experiment_id),
        OptimizationTrialRow.study_id == str(key.study_id),
        OptimizationTrialRow.trial_number == key.trial_number,
    )


def _draft_matches(entry: OptimizationTrialEntry, draft: OptimizationTrialDraft) -> bool:
    return (
        entry.experiment_id == draft.experiment_id
        and entry.plan_id == draft.plan_id
        and entry.plan_hash == draft.plan_hash
        and entry.trial == draft.trial
        and entry.benchmark_run_id == draft.benchmark_run_id
        and entry.reservation == draft.reservation
        and entry.checkpoint is None
        and entry.evidence_run is None
    )


class SqlAlchemyOptimizationTrialRepository:
    """Persist Trial facts without owning caller transaction boundaries."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_suggested(self, draft: OptimizationTrialDraft) -> OptimizationTrialEntry:
        self._require_approved_plan(draft)
        existing = self._session.scalar(_key_statement(draft.key()))
        if existing is not None:
            entry = _entry_from_row(existing)
            if _draft_matches(entry, draft):
                return entry
            raise OptimizationTrialConflictError(TRIAL_CONFLICT)
        now = _database_now(self._session)
        trial = draft.trial
        statement = (
            insert(OptimizationTrialRow)
            .values(
                trial_id=str(trial.trial_id),
                schema_version="optimization-trial-entry/v1",
                trial_schema_version=trial.schema_version,
                experiment_id=str(draft.experiment_id),
                plan_id=str(draft.plan_id),
                plan_hash=str(draft.plan_hash),
                study_id=str(trial.study_id),
                trial_number=trial.trial_number,
                candidate_id=str(trial.candidate_id),
                benchmark_run_id=str(draft.benchmark_run_id),
                parameters_json=_json_object(trial.parameters),
                status=trial.status.value,
                constraints_json=[],
                objective_json=None,
                provenance_json=None,
                evidence_json=[],
                error_json=None,
                reservation_json=_json_object(draft.reservation),
                checkpoint_stage=None,
                provider_resource_id=None,
                evidence_run_json=None,
                created_at=now,
                updated_at=now,
                ended_at=None,
                version=1,
            )
            .on_conflict_do_nothing()
            .returning(OptimizationTrialRow.trial_id)
        )
        inserted = self._session.execute(statement).scalar_one_or_none()
        row = self._session.scalar(_key_statement(draft.key()))
        if row is None:
            raise OptimizationTrialConflictError(TRIAL_CONFLICT)
        entry = _entry_from_row(row)
        if inserted is None and not _draft_matches(entry, draft):
            raise OptimizationTrialConflictError(TRIAL_CONFLICT)
        return entry

    def mark_pending(
        self,
        key: OptimizationTrialKey,
        checkpoint: OptimizationTrialCheckpoint,
    ) -> OptimizationTrialEntry:
        row = self._require_locked(key)
        entry = _entry_from_row(row)
        if entry.trial.status is not TrialStatus.SUGGESTED:
            raise OptimizationTrialConflictError(TRIAL_STATE_CONFLICT)
        if entry.checkpoint == checkpoint:
            return entry
        row.checkpoint_stage = checkpoint.stage.value
        row.provider_resource_id = checkpoint.provider_resource_id
        row.updated_at = _database_now(self._session)
        row.version += 1
        self._session.flush()
        return _entry_from_row(row)

    def complete(
        self,
        key: OptimizationTrialKey,
        completion: OptimizationTrialCompletion,
    ) -> OptimizationTrialEntry:
        row = self._require_locked(key)
        entry = _entry_from_row(row)
        if entry.trial.status is not TrialStatus.SUGGESTED:
            if entry.trial == completion.trial and entry.evidence_run == completion.evidence_run:
                return entry
            raise OptimizationTrialConflictError(TRIAL_STATE_CONFLICT)
        trial = completion.trial
        if (
            trial.trial_id != entry.trial.trial_id
            or trial.study_id != entry.trial.study_id
            or trial.trial_number != entry.trial.trial_number
            or trial.candidate_id != entry.trial.candidate_id
            or trial.parameters != entry.trial.parameters
        ):
            raise OptimizationTrialConflictError(TRIAL_CONFLICT)
        now = _database_now(self._session)
        row.status = trial.status.value
        row.constraints_json = cast(
            list[JsonValue],
            [item.model_dump(mode="json") for item in trial.constraints],
        )
        row.objective_json = _json_object(trial.objective) if trial.objective is not None else None
        row.provenance_json = (
            _json_object(trial.provenance) if trial.provenance is not None else None
        )
        row.evidence_json = cast(
            list[JsonValue],
            [item.model_dump(mode="json") for item in trial.evidence],
        )
        row.error_json = _json_object(trial.error) if trial.error is not None else None
        row.checkpoint_stage = None
        row.provider_resource_id = None
        row.evidence_run_json = _json_object(completion.evidence_run)
        row.updated_at = now
        row.ended_at = now
        row.version += 1
        self._session.flush()
        return _entry_from_row(row)

    def get(self, key: OptimizationTrialKey) -> OptimizationTrialEntry | None:
        row = self._session.scalar(_key_statement(key))
        return _entry_from_row(row) if row is not None else None

    def list_for_study(
        self,
        *,
        experiment_id: ExperimentId,
        study_id: StudyId,
        plan_hash: PlanHash,
    ) -> tuple[OptimizationTrialEntry, ...]:
        statement = (
            select(OptimizationTrialRow)
            .where(
                OptimizationTrialRow.experiment_id == str(experiment_id),
                OptimizationTrialRow.study_id == str(study_id),
                OptimizationTrialRow.plan_hash == str(plan_hash),
            )
            .order_by(OptimizationTrialRow.trial_number)
        )
        return tuple(_entry_from_row(row) for row in self._session.scalars(statement))

    def _require_locked(self, key: OptimizationTrialKey) -> OptimizationTrialRow:
        row = cast(
            OptimizationTrialRow | None,
            self._session.scalar(_key_statement(key).with_for_update()),
        )
        if row is None:
            raise OptimizationTrialNotFoundError(TRIAL_NOT_FOUND)
        return row

    def _require_approved_plan(self, draft: OptimizationTrialDraft) -> None:
        statement = select(PlanRow).where(
            PlanRow.experiment_id == str(draft.experiment_id),
            PlanRow.id == str(draft.plan_id),
            PlanRow.plan_hash == str(draft.plan_hash),
            PlanRow.kind == PlanKind.OPTIMIZATION.value,
            PlanRow.status == PlanStatus.APPROVED.value,
        )
        if self._session.scalar(statement) is None:
            raise OptimizationTrialConflictError(PLAN_BINDING_INVALID)


__all__ = ["SqlAlchemyOptimizationTrialRepository"]
