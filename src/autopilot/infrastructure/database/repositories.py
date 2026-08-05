"""Transactional PostgreSQL repositories for Jobs, leases, events, and audit."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import Select, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from autopilot.domain.artifacts import ArtifactProducer, ArtifactRef
from autopilot.domain.enums import JobStatus
from autopilot.domain.errors import ErrorEnvelope
from autopilot.domain.identifiers import (
    ArtifactId,
    EventId,
    ExperimentId,
    JobId,
    Sha256Digest,
    ToolName,
    WorkerId,
)
from autopilot.domain.jobs import TERMINAL_JOB_STATUSES, JobProgress, JobRecord
from autopilot.evidence.models import ArtifactMetadata
from autopilot.evidence.ports import ArtifactRepository
from autopilot.infrastructure.database.errors import (
    ArtifactBindingError,
    IdempotencyConflictError,
    JobNotFoundError,
    JobStateConflictError,
    LeaseConflictError,
    PersistenceError,
    PlanBindingError,
)
from autopilot.infrastructure.database.models import (
    ArtifactRow,
    AuditEventRow,
    EventRow,
    IdempotencyRow,
    JobRow,
    PlanRow,
)
from autopilot.jobs.models import (
    AuditEvent,
    ClaimedJob,
    EnqueueJobResult,
    JobEvent,
    JobEventType,
    JobLease,
    JobTransition,
)
from autopilot.jobs.service import transition_job

JOB_NOT_FOUND: Final = "Job does not exist"
IDEMPOTENCY_MISMATCH: Final = "idempotency key is already bound to different input"
INVALID_LEASE_DURATION: Final = "lease duration must be positive"
LEASE_NOT_OWNED: Final = "Job lease is not active for this Worker"
LEASE_FENCING_MISMATCH: Final = "Job lease fencing token is stale"
WORKER_REQUIRED: Final = "worker-owned Job transition requires an active lease"
RUNNING_REQUIRED: Final = "Job progress can only be updated while running"
QUEUED_REQUIRED: Final = "only queued Jobs can be enqueued"
PLAN_BINDING_MISMATCH: Final = (
    "Job Plan must be approved, belong to the same Experiment, and match Job kind"
)
ARTIFACT_BINDING_MISMATCH: Final = "Job result Artifact does not match persisted metadata"
CANCELLATION_TERMINAL: Final = "terminal Jobs cannot accept a cancellation request"
DATABASE_TIME_INVALID: Final = "PostgreSQL did not return an aware database timestamp"

CLAIMABLE_STATUSES: Final = (
    JobStatus.QUEUED.value,
    JobStatus.VALIDATING.value,
    JobStatus.WAITING_APPROVAL.value,
    JobStatus.RUNNING.value,
)
WORKER_OWNED_TARGETS: Final = frozenset(
    {
        JobStatus.VALIDATING,
        JobStatus.RUNNING,
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.TIMED_OUT,
    }
)


def _lease_expiry(now: datetime, lease_duration: timedelta) -> datetime:
    if lease_duration <= timedelta(0):
        raise ValueError(INVALID_LEASE_DURATION)
    return now + lease_duration


def _database_now(session: Session) -> datetime:
    value: object = session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PersistenceError(DATABASE_TIME_INVALID)
    return value


def _artifact_metadata(row: ArtifactRow) -> ArtifactMetadata:
    return ArtifactMetadata(
        schema_version=row.schema_version,
        artifact_id=ArtifactId(root=row.id),
        experiment_id=row.experiment_id,
        category=row.category,
        sha256=Sha256Digest(root=row.sha256),
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        producer=ArtifactProducer(
            component=row.producer_component,
            version=row.producer_version,
        ),
        storage_path=row.storage_path,
        created_at=row.created_at,
    )


def _artifact_ref(row: ArtifactRow) -> ArtifactRef:
    return _artifact_metadata(row).to_ref()


def _job_record(session: Session, row: JobRow) -> JobRecord:
    result_artifact = None
    if row.result_artifact_id is not None:
        artifact = session.get(ArtifactRow, row.result_artifact_id)
        if artifact is None or artifact.experiment_id != row.experiment_id:
            raise ArtifactBindingError(ARTIFACT_BINDING_MISMATCH)
        result_artifact = _artifact_ref(artifact)
    return JobRecord(
        schema_version=row.schema_version,
        job_id=JobId(root=row.id),
        experiment_id=row.experiment_id,
        plan_id=row.plan_id,
        kind=row.kind,
        status=row.status,
        provider_job_id=row.provider_job_id,
        progress=(JobProgress.model_validate(row.progress_json) if row.progress_json else None),
        submitted_at=row.submitted_at,
        started_at=row.started_at,
        ended_at=row.ended_at,
        cancel_requested_at=row.cancel_requested_at,
        result_artifact=result_artifact,
        error=(ErrorEnvelope.model_validate(row.error_json) if row.error_json else None),
    )


def _lease(row: JobRow, recovered: bool) -> JobLease:
    if (
        row.lease_owner is None
        or row.lease_schema_version is None
        or row.lease_acquired_at is None
        or row.lease_heartbeat_at is None
        or row.lease_expires_at is None
    ):
        raise LeaseConflictError(LEASE_NOT_OWNED)
    return JobLease(
        schema_version=row.lease_schema_version,
        job_id=JobId(root=row.id),
        worker_id=WorkerId(root=row.lease_owner),
        acquired_at=row.lease_acquired_at,
        heartbeat_at=row.lease_heartbeat_at,
        expires_at=row.lease_expires_at,
        fencing_token=row.lease_generation,
        recovered=recovered,
    )


def _event_type(target: JobStatus) -> JobEventType:
    return {
        JobStatus.RUNNING: JobEventType.STARTED,
        JobStatus.SUCCEEDED: JobEventType.COMPLETED,
        JobStatus.FAILED: JobEventType.FAILED,
        JobStatus.CANCELLED: JobEventType.CANCELLED,
        JobStatus.TIMED_OUT: JobEventType.TIMED_OUT,
    }.get(target, JobEventType.STATUS_CHANGED)


def _append_event(
    session: Session,
    row: JobRow,
    *,
    event_type: JobEventType,
    occurred_at: datetime,
    previous_status: JobStatus | None,
) -> None:
    session.add(
        EventRow(
            event_id=str(EventId.new()),
            schema_version="job-event/v1",
            experiment_id=row.experiment_id,
            job_id=row.id,
            event_type=event_type.value,
            occurred_at=occurred_at,
            previous_status=previous_status.value if previous_status is not None else None,
            current_status=row.status,
            payload_json={},
        )
    )


def _locked_job_statement(job_id: JobId) -> Select[tuple[JobRow]]:
    return select(JobRow).where(JobRow.id == str(job_id)).with_for_update()


def _idempotent_replay(
    session: Session,
    existing: IdempotencyRow,
    *,
    job: JobRecord,
    request_hash: Sha256Digest,
    action: ToolName,
) -> EnqueueJobResult:
    if (
        existing.request_hash != str(request_hash)
        or existing.action != str(action)
        or existing.experiment_id != str(job.experiment_id)
    ):
        raise IdempotencyConflictError(IDEMPOTENCY_MISMATCH)
    existing_job = session.get(JobRow, existing.job_id)
    if existing_job is None:
        raise JobNotFoundError(JOB_NOT_FOUND)
    restored = _job_record(session, existing_job)
    if restored.plan_id != job.plan_id or restored.kind is not job.kind:
        raise IdempotencyConflictError(IDEMPOTENCY_MISMATCH)
    return EnqueueJobResult(job=restored, created=False)


def claimable_job_statement() -> Select[tuple[JobRow]]:
    """Build the PostgreSQL Lease Queue claim query for contract verification."""
    return (
        select(JobRow)
        .where(
            JobRow.status.in_(CLAIMABLE_STATUSES),
            or_(
                JobRow.lease_owner.is_(None),
                JobRow.lease_expires_at <= func.clock_timestamp(),
            ),
        )
        .order_by(JobRow.submitted_at, JobRow.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )


def _require_worker_lease(
    row: JobRow,
    *,
    worker_id: WorkerId,
    fencing_token: int,
    now: datetime,
) -> None:
    if (
        row.lease_owner != str(worker_id)
        or row.lease_expires_at is None
        or row.lease_expires_at <= now
    ):
        raise LeaseConflictError(LEASE_NOT_OWNED)
    if row.lease_generation != fencing_token:
        raise LeaseConflictError(LEASE_FENCING_MISMATCH)


class SqlAlchemyArtifactRepository(ArtifactRepository):
    """Persist filesystem metadata without exposing paths through domain references."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, metadata: ArtifactMetadata) -> None:
        existing = self._session.get(ArtifactRow, str(metadata.artifact_id))
        if existing is not None:
            if _artifact_metadata(existing) != metadata:
                raise ArtifactBindingError(ARTIFACT_BINDING_MISMATCH)
            return
        statement = (
            insert(ArtifactRow)
            .values(
                id=str(metadata.artifact_id),
                schema_version=metadata.schema_version,
                experiment_id=str(metadata.experiment_id),
                category=metadata.category,
                content_type=metadata.content_type,
                size_bytes=metadata.size_bytes,
                sha256=str(metadata.sha256),
                producer_component=metadata.producer.component,
                producer_version=metadata.producer.version,
                storage_path=metadata.storage_path,
                created_at=metadata.created_at,
            )
            .on_conflict_do_nothing(index_elements=[ArtifactRow.id])
            .returning(ArtifactRow.id)
        )
        inserted_id = self._session.execute(statement).scalar_one_or_none()
        if inserted_id is not None:
            return
        existing = self._session.get(ArtifactRow, str(metadata.artifact_id))
        if existing is None or _artifact_metadata(existing) != metadata:
            raise ArtifactBindingError(ARTIFACT_BINDING_MISMATCH)

    def get(
        self,
        artifact_id: ArtifactId,
        *,
        experiment_id: ExperimentId,
    ) -> ArtifactMetadata | None:
        row = self._session.get(ArtifactRow, str(artifact_id))
        if row is None or row.experiment_id != str(experiment_id):
            return None
        return _artifact_metadata(row)


class SqlAlchemyJobRepository:
    """Use one caller-owned Session so every operation composes into one transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def enqueue(
        self,
        job: JobRecord,
        *,
        idempotency_key: Sha256Digest,
        request_hash: Sha256Digest,
        action: ToolName,
    ) -> EnqueueJobResult:
        if job.status is not JobStatus.QUEUED:
            raise ValueError(QUEUED_REQUIRED)
        existing = self._session.get(IdempotencyRow, str(idempotency_key))
        if existing is not None:
            return _idempotent_replay(
                self._session,
                existing,
                job=job,
                request_hash=request_hash,
                action=action,
            )

        plan = self._session.get(PlanRow, str(job.plan_id))
        if (
            plan is None
            or plan.experiment_id != str(job.experiment_id)
            or plan.kind != job.kind.value
            or plan.status != "approved"
        ):
            raise PlanBindingError(PLAN_BINDING_MISMATCH)
        statement = (
            insert(IdempotencyRow)
            .values(
                idempotency_key=str(idempotency_key),
                schema_version="idempotency-record/v1",
                request_hash=str(request_hash),
                action=str(action),
                experiment_id=str(job.experiment_id),
                job_id=str(job.job_id),
                created_at=job.submitted_at,
            )
            .on_conflict_do_nothing(index_elements=[IdempotencyRow.idempotency_key])
            .returning(IdempotencyRow.idempotency_key)
        )
        inserted_key = self._session.execute(statement).scalar_one_or_none()
        if inserted_key is None:
            existing = self._session.get(IdempotencyRow, str(idempotency_key))
            if existing is None:
                raise IdempotencyConflictError(IDEMPOTENCY_MISMATCH)
            return _idempotent_replay(
                self._session,
                existing,
                job=job,
                request_hash=request_hash,
                action=action,
            )

        row = JobRow(
            id=str(job.job_id),
            schema_version=job.schema_version,
            experiment_id=str(job.experiment_id),
            plan_id=str(job.plan_id),
            kind=job.kind.value,
            status=job.status.value,
            provider_job_id=job.provider_job_id,
            progress_json=job.progress.model_dump(mode="json") if job.progress else None,
            error_json=None,
            result_artifact_id=None,
            submitted_at=job.submitted_at,
            started_at=None,
            ended_at=None,
            cancel_requested_at=job.cancel_requested_at,
            lease_schema_version=None,
            lease_generation=0,
        )
        self._session.add(row)
        self._session.flush()
        _append_event(
            self._session,
            row,
            event_type=JobEventType.QUEUED,
            occurred_at=job.submitted_at,
            previous_status=None,
        )
        self._session.flush()
        return EnqueueJobResult(job=job, created=True)

    def get(self, job_id: JobId) -> JobRecord | None:
        row = self._session.get(JobRow, str(job_id))
        return _job_record(self._session, row) if row is not None else None

    def claim_next(
        self,
        *,
        worker_id: WorkerId,
        lease_duration: timedelta,
    ) -> ClaimedJob | None:
        if lease_duration <= timedelta(0):
            raise ValueError(INVALID_LEASE_DURATION)
        row = self._session.scalar(claimable_job_statement())
        if row is None:
            return None
        now = _database_now(self._session)
        expires_at = _lease_expiry(now, lease_duration)
        recovered = row.lease_generation > 0
        row.lease_schema_version = "job-lease/v1"
        row.lease_owner = str(worker_id)
        row.lease_acquired_at = now
        row.lease_heartbeat_at = now
        row.lease_expires_at = expires_at
        row.lease_generation += 1
        row.version += 1
        self._session.flush()
        if recovered:
            _append_event(
                self._session,
                row,
                event_type=JobEventType.LEASE_RECOVERED,
                occurred_at=now,
                previous_status=None,
            )
            self._session.flush()
        return ClaimedJob(
            job=_job_record(self._session, row),
            lease=_lease(row, recovered),
        )

    def heartbeat(
        self,
        *,
        job_id: JobId,
        worker_id: WorkerId,
        fencing_token: int,
        lease_duration: timedelta,
    ) -> ClaimedJob:
        if lease_duration <= timedelta(0):
            raise ValueError(INVALID_LEASE_DURATION)
        row = self._session.scalar(_locked_job_statement(job_id))
        if row is None:
            raise JobNotFoundError(JOB_NOT_FOUND)
        now = _database_now(self._session)
        expires_at = _lease_expiry(now, lease_duration)
        _require_worker_lease(
            row,
            worker_id=worker_id,
            fencing_token=fencing_token,
            now=now,
        )
        row.lease_heartbeat_at = now
        row.lease_expires_at = expires_at
        row.version += 1
        self._session.flush()
        return ClaimedJob(job=_job_record(self._session, row), lease=_lease(row, False))

    def transition(
        self,
        *,
        job_id: JobId,
        transition: JobTransition,
        worker_id: WorkerId | None = None,
        fencing_token: int | None = None,
    ) -> JobRecord:
        row = self._session.scalar(_locked_job_statement(job_id))
        if row is None:
            raise JobNotFoundError(JOB_NOT_FOUND)
        target = transition.target
        occurred_at = _database_now(self._session)
        if target in WORKER_OWNED_TARGETS or row.lease_owner is not None:
            if worker_id is None or fencing_token is None:
                raise LeaseConflictError(WORKER_REQUIRED)
            _require_worker_lease(
                row,
                worker_id=worker_id,
                fencing_token=fencing_token,
                now=occurred_at,
            )

        current = _job_record(self._session, row)
        persisted_transition = transition.model_copy(update={"occurred_at": occurred_at})
        updated = transition_job(current, persisted_transition)
        previous_status = current.status
        if updated.result_artifact is not None:
            artifact = self._session.get(ArtifactRow, str(updated.result_artifact.artifact_id))
            if (
                artifact is None
                or artifact.experiment_id != row.experiment_id
                or _artifact_ref(artifact) != updated.result_artifact
            ):
                raise ArtifactBindingError(ARTIFACT_BINDING_MISMATCH)
        row.status = updated.status.value
        row.provider_job_id = updated.provider_job_id
        row.progress_json = updated.progress.model_dump(mode="json") if updated.progress else None
        row.started_at = updated.started_at
        row.ended_at = updated.ended_at
        row.error_json = updated.error.model_dump(mode="json") if updated.error else None
        row.result_artifact_id = (
            str(updated.result_artifact.artifact_id) if updated.result_artifact else None
        )
        row.version += 1
        if target in TERMINAL_JOB_STATUSES:
            row.lease_schema_version = None
            row.lease_owner = None
            row.lease_acquired_at = None
            row.lease_heartbeat_at = None
            row.lease_expires_at = None
        self._session.flush()
        _append_event(
            self._session,
            row,
            event_type=_event_type(target),
            occurred_at=occurred_at,
            previous_status=previous_status,
        )
        self._session.flush()
        return updated

    def update_progress(
        self,
        *,
        job_id: JobId,
        worker_id: WorkerId,
        fencing_token: int,
        progress: JobProgress,
    ) -> JobRecord:
        row = self._session.scalar(_locked_job_statement(job_id))
        if row is None:
            raise JobNotFoundError(JOB_NOT_FOUND)
        occurred_at = _database_now(self._session)
        _require_worker_lease(
            row,
            worker_id=worker_id,
            fencing_token=fencing_token,
            now=occurred_at,
        )
        if row.status != JobStatus.RUNNING.value:
            raise LeaseConflictError(RUNNING_REQUIRED)
        row.progress_json = progress.model_dump(mode="json")
        row.version += 1
        self._session.flush()
        _append_event(
            self._session,
            row,
            event_type=JobEventType.PROGRESS,
            occurred_at=occurred_at,
            previous_status=None,
        )
        self._session.flush()
        return _job_record(self._session, row)

    def request_cancel(self, *, job_id: JobId) -> JobRecord:
        row = self._session.scalar(_locked_job_statement(job_id))
        if row is None:
            raise JobNotFoundError(JOB_NOT_FOUND)
        if JobStatus(row.status) in TERMINAL_JOB_STATUSES:
            raise JobStateConflictError(CANCELLATION_TERMINAL)
        if row.cancel_requested_at is not None:
            return _job_record(self._session, row)
        occurred_at = _database_now(self._session)
        row.cancel_requested_at = occurred_at
        row.version += 1
        self._session.flush()
        _append_event(
            self._session,
            row,
            event_type=JobEventType.CANCEL_REQUESTED,
            occurred_at=occurred_at,
            previous_status=None,
        )
        self._session.flush()
        return _job_record(self._session, row)

    def list_events(self, job_id: JobId) -> tuple[JobEvent, ...]:
        rows = self._session.scalars(
            select(EventRow).where(EventRow.job_id == str(job_id)).order_by(EventRow.sequence)
        )
        return tuple(
            JobEvent(
                schema_version=row.schema_version,
                event_id=row.event_id,
                sequence=row.sequence,
                experiment_id=row.experiment_id,
                job_id=row.job_id,
                event_type=row.event_type,
                occurred_at=row.occurred_at,
                previous_status=row.previous_status,
                current_status=row.current_status,
                payload=row.payload_json,
            )
            for row in rows
        )


class SqlAlchemyAuditRepository:
    """Append audit records; the database trigger rejects UPDATE and DELETE."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append(self, event: AuditEvent) -> None:
        self._session.add(
            AuditEventRow(
                id=str(event.audit_event_id),
                schema_version=event.schema_version,
                experiment_id=(str(event.experiment_id) if event.experiment_id else None),
                job_id=str(event.job_id) if event.job_id else None,
                actor=event.actor,
                action=event.action,
                resource_type=event.resource_type,
                resource_id=event.resource_id,
                request_id=event.request_id,
                decision_id=event.decision_id,
                before_artifact_id=(
                    str(event.before_artifact_id) if event.before_artifact_id else None
                ),
                after_artifact_id=(
                    str(event.after_artifact_id) if event.after_artifact_id else None
                ),
                result=event.result.value,
                occurred_at=event.occurred_at,
            )
        )
        self._session.flush()
