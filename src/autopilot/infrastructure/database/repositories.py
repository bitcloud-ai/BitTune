"""Transactional PostgreSQL repositories for Jobs, leases, events, and audit."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import Select, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from autopilot.domain.approvals import (
    ApprovalExecutionBinding,
    ApprovalRecord,
    validate_approval_for_execution,
)
from autopilot.domain.artifacts import ArtifactProducer, ArtifactRef
from autopilot.domain.enums import (
    ApprovalDecision,
    JobStatus,
    PlanStatus,
    RiskLevel,
    UserRole,
)
from autopilot.domain.errors import ErrorEnvelope
from autopilot.domain.identifiers import (
    ApprovalId,
    ArtifactId,
    EventId,
    ExperimentId,
    JobId,
    PlanHash,
    PlanId,
    Sha256Digest,
    ToolName,
    UserId,
    WorkerId,
)
from autopilot.domain.identities import HumanSubject, SubjectKind
from autopilot.domain.jobs import TERMINAL_JOB_STATUSES, JobProgress, JobRecord
from autopilot.evidence.models import ArtifactMetadata
from autopilot.evidence.ports import ArtifactRepository
from autopilot.gateway.approval_ports import (
    ApprovalRepository,
    CreateApprovalRequest,
    DecideApprovalRequest,
)
from autopilot.gateway.models import JobAuthorizationDraft, bind_job_authorization
from autopilot.infrastructure.database.errors import (
    ApprovalActorConflictError,
    ApprovalBindingError,
    ApprovalNotFoundError,
    ApprovalStateConflictError,
    ArtifactBindingError,
    IdempotencyConflictError,
    JobNotFoundError,
    JobStateConflictError,
    LeaseConflictError,
    PersistenceError,
    PlanBindingError,
)
from autopilot.infrastructure.database.gateway_repositories import (
    SqlAlchemyJobAuthorizationRepository,
    replay_authorization_matches,
)
from autopilot.infrastructure.database.models import (
    ApprovalRow,
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
APPROVAL_NOT_FOUND: Final = "Approval does not exist"
APPROVAL_PLAN_BINDING_MISMATCH: Final = (
    "Approval must reference the current immutable L2 Plan material"
)
APPROVAL_REQUEST_CONFLICT: Final = "Approval request already exists for another requester"
APPROVAL_NOT_PENDING: Final = "Approval has already reached a terminal decision"
APPROVAL_PLAN_NOT_DRAFT: Final = "only a draft L2 Plan can be approved or rejected"
APPROVAL_SELF_DECISION: Final = "requesters cannot decide their own Approval"
APPROVAL_DECISION_INVALID: Final = "human Approval decisions must be approved or rejected"
APPROVAL_EXPIRY_INVALID: Final = "Approval lifetime must be positive"
APPROVAL_EXECUTION_STATE_INVALID: Final = "Approval is not active for execution"
APPROVAL_ACTOR_INVALID: Final = "Approval requires an independent human admin decision"

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


def _human_subject(*, kind: str, user_id: str, role: str) -> HumanSubject:
    return HumanSubject(
        kind=SubjectKind(kind),
        user_id=UserId(root=user_id),
        role=UserRole(role),
    )


def _approval_record(row: ApprovalRow) -> ApprovalRecord:
    decided_by = None
    if row.decided_by_id is not None:
        if row.decided_by_kind is None or row.decided_by_role is None:
            raise ApprovalActorConflictError(APPROVAL_ACTOR_INVALID)
        decided_by = _human_subject(
            kind=row.decided_by_kind,
            user_id=row.decided_by_id,
            role=row.decided_by_role,
        )
    return ApprovalRecord(
        schema_version=row.schema_version,
        approval_id=ApprovalId(root=row.id),
        experiment_id=ExperimentId(root=row.experiment_id),
        plan_id=PlanId(root=row.plan_id),
        plan_hash=PlanHash(root=row.plan_hash),
        action=ToolName(root=row.action),
        risk_level=row.risk_level,
        requester=_human_subject(
            kind=row.requester_kind,
            user_id=row.requester_id,
            role=row.requester_role,
        ),
        requested_at=row.requested_at,
        expires_at=row.expires_at,
        decision=row.decision,
        decided_by=decided_by,
        decided_at=row.decided_at,
        comment=row.comment,
    )


def _require_approval_plan(
    row: PlanRow | None,
    *,
    experiment_id: ExperimentId,
    plan_id: PlanId,
    expected_plan_hash: PlanHash,
) -> PlanRow:
    if (
        row is None
        or row.experiment_id != str(experiment_id)
        or row.id != str(plan_id)
        or row.plan_hash != str(expected_plan_hash)
        or row.risk_level != RiskLevel.L2.value
    ):
        raise ApprovalBindingError(APPROVAL_PLAN_BINDING_MISMATCH)
    return row


def _expire_approval(session: Session, row: ApprovalRow, *, now: datetime) -> ApprovalRecord:
    if row.decision != ApprovalDecision.PENDING.value or now < row.expires_at:
        return _approval_record(row)
    row.decision = ApprovalDecision.EXPIRED.value
    row.decided_by_kind = None
    row.decided_by_id = None
    row.decided_by_role = None
    row.decided_at = None
    session.flush()
    return _approval_record(row)


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


def _locked_plan_statement(
    *,
    experiment_id: ExperimentId,
    plan_id: PlanId,
) -> Select[tuple[PlanRow]]:
    return (
        select(PlanRow)
        .where(
            PlanRow.experiment_id == str(experiment_id),
            PlanRow.id == str(plan_id),
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )


def _locked_approval_statement(approval_id: ApprovalId) -> Select[tuple[ApprovalRow]]:
    return (
        select(ApprovalRow)
        .where(ApprovalRow.id == str(approval_id))
        .execution_options(populate_existing=True)
        .with_for_update()
    )


def _locked_plan_approval_statement(
    *,
    experiment_id: ExperimentId,
    plan_id: PlanId,
    expected_plan_hash: PlanHash,
    action: ToolName,
) -> Select[tuple[ApprovalRow]]:
    return (
        select(ApprovalRow)
        .where(
            ApprovalRow.experiment_id == str(experiment_id),
            ApprovalRow.plan_id == str(plan_id),
            ApprovalRow.plan_hash == str(expected_plan_hash),
            ApprovalRow.action == str(action),
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )


def _idempotent_replay(
    session: Session,
    existing: IdempotencyRow,
    *,
    job: JobRecord,
    authorization: JobAuthorizationDraft,
) -> EnqueueJobResult:
    if (
        existing.request_hash != str(authorization.request_hash)
        or existing.action != str(authorization.action)
        or existing.experiment_id != str(job.experiment_id)
    ):
        raise IdempotencyConflictError(IDEMPOTENCY_MISMATCH)
    existing_job = session.get(JobRow, existing.job_id)
    if existing_job is None:
        raise JobNotFoundError(JOB_NOT_FOUND)
    restored = _job_record(session, existing_job)
    if restored.plan_id != job.plan_id or restored.kind is not job.kind:
        raise IdempotencyConflictError(IDEMPOTENCY_MISMATCH)
    persisted_authorization = SqlAlchemyJobAuthorizationRepository(session).get(restored.job_id)
    if persisted_authorization is None or not replay_authorization_matches(
        persisted_authorization,
        authorization,
    ):
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


class SqlAlchemyApprovalRepository(ApprovalRepository):
    """Persist L2 approval decisions without owning transaction boundaries."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, request: CreateApprovalRequest) -> ApprovalRecord:
        if request.expires_in <= timedelta(0):
            raise ValueError(APPROVAL_EXPIRY_INVALID)
        plan = _require_approval_plan(
            self._session.scalar(
                _locked_plan_statement(
                    experiment_id=request.experiment_id,
                    plan_id=request.plan_id,
                )
            ),
            experiment_id=request.experiment_id,
            plan_id=request.plan_id,
            expected_plan_hash=request.expected_plan_hash,
        )
        binding_statement = _locked_plan_approval_statement(
            experiment_id=request.experiment_id,
            plan_id=request.plan_id,
            expected_plan_hash=request.expected_plan_hash,
            action=request.action,
        )
        existing = self._session.scalar(binding_statement)
        now = _database_now(self._session)
        if existing is not None:
            if (
                existing.requester_kind != request.requester.kind.value
                or existing.requester_id != str(request.requester.user_id)
                or existing.requester_role != request.requester.role.value
            ):
                raise ApprovalStateConflictError(APPROVAL_REQUEST_CONFLICT)
            return _expire_approval(self._session, existing, now=now)
        if plan.status != PlanStatus.DRAFT.value:
            raise ApprovalStateConflictError(APPROVAL_PLAN_NOT_DRAFT)

        record = ApprovalRecord(
            approval_id=request.approval_id,
            experiment_id=request.experiment_id,
            plan_id=request.plan_id,
            plan_hash=request.expected_plan_hash,
            action=request.action,
            requester=request.requester,
            requested_at=now,
            expires_at=now + request.expires_in,
            comment=request.comment,
        )
        statement = (
            insert(ApprovalRow)
            .values(
                id=str(record.approval_id),
                schema_version=record.schema_version,
                experiment_id=str(record.experiment_id),
                plan_id=str(record.plan_id),
                plan_hash=str(record.plan_hash),
                action=str(record.action),
                risk_level=record.risk_level.value,
                requester_kind=record.requester.kind.value,
                requester_id=str(record.requester.user_id),
                requester_role=record.requester.role.value,
                requested_at=record.requested_at,
                expires_at=record.expires_at,
                decision=record.decision.value,
                decided_by_kind=None,
                decided_by_id=None,
                decided_by_role=None,
                decided_at=None,
                comment=record.comment,
            )
            .on_conflict_do_nothing()
            .returning(ApprovalRow.id)
        )
        inserted_id = self._session.execute(statement).scalar_one_or_none()
        if inserted_id is not None:
            self._session.flush()
            return record
        existing = self._session.scalar(binding_statement)
        if (
            existing is None
            or existing.requester_kind != request.requester.kind.value
            or existing.requester_id != str(request.requester.user_id)
            or existing.requester_role != request.requester.role.value
        ):
            raise ApprovalStateConflictError(APPROVAL_REQUEST_CONFLICT)
        return _expire_approval(self._session, existing, now=now)

    def get(self, approval_id: ApprovalId) -> ApprovalRecord | None:
        row = self._session.scalar(_locked_approval_statement(approval_id))
        if row is None:
            return None
        return _expire_approval(self._session, row, now=_database_now(self._session))

    def get_for_plan(
        self,
        *,
        experiment_id: ExperimentId,
        plan_id: PlanId,
        expected_plan_hash: PlanHash,
        action: ToolName,
    ) -> ApprovalRecord | None:
        row = self._session.scalar(
            _locked_plan_approval_statement(
                experiment_id=experiment_id,
                plan_id=plan_id,
                expected_plan_hash=expected_plan_hash,
                action=action,
            )
        )
        if row is None:
            return None
        return _expire_approval(self._session, row, now=_database_now(self._session))

    def require_valid_for_execution(
        self,
        *,
        approval_id: ApprovalId,
        experiment_id: ExperimentId,
        plan_id: PlanId,
        expected_plan_hash: PlanHash,
        action: ToolName,
    ) -> ApprovalRecord:
        row = self._session.scalar(_locked_approval_statement(approval_id))
        if row is None:
            raise ApprovalNotFoundError(APPROVAL_NOT_FOUND)
        if (
            row.experiment_id != str(experiment_id)
            or row.plan_id != str(plan_id)
            or row.plan_hash != str(expected_plan_hash)
            or row.action != str(action)
        ):
            raise ApprovalBindingError(APPROVAL_PLAN_BINDING_MISMATCH)
        now = _database_now(self._session)
        if row.decision != ApprovalDecision.APPROVED.value or now >= row.expires_at:
            raise ApprovalStateConflictError(APPROVAL_EXECUTION_STATE_INVALID)
        if (
            row.requester_kind != SubjectKind.HUMAN.value
            or row.requester_role not in {UserRole.OPERATOR.value, UserRole.ADMIN.value}
            or row.decided_by_kind != SubjectKind.HUMAN.value
            or row.decided_by_role != UserRole.ADMIN.value
            or row.decided_by_id is None
            or row.decided_by_id == row.requester_id
        ):
            raise ApprovalActorConflictError(APPROVAL_ACTOR_INVALID)
        record = _approval_record(row)
        try:
            validate_approval_for_execution(
                record,
                ApprovalExecutionBinding(
                    experiment_id=experiment_id,
                    plan_id=plan_id,
                    plan_hash=expected_plan_hash,
                    action=action,
                ),
                now,
            )
        except ValueError as error:
            raise ApprovalStateConflictError(APPROVAL_EXECUTION_STATE_INVALID) from error
        return record

    def decide(self, request: DecideApprovalRequest) -> ApprovalRecord:
        if request.decision not in {ApprovalDecision.APPROVED, ApprovalDecision.REJECTED}:
            raise ValueError(APPROVAL_DECISION_INVALID)

        plan = _require_approval_plan(
            self._session.scalar(
                _locked_plan_statement(
                    experiment_id=request.experiment_id,
                    plan_id=request.expected_plan_id,
                )
            ),
            experiment_id=request.experiment_id,
            plan_id=request.expected_plan_id,
            expected_plan_hash=request.expected_plan_hash,
        )
        row = self._session.scalar(_locked_approval_statement(request.approval_id))
        if row is None:
            raise ApprovalNotFoundError(APPROVAL_NOT_FOUND)
        if (
            row.experiment_id != str(request.experiment_id)
            or row.plan_id != str(request.expected_plan_id)
            or row.plan_hash != str(request.expected_plan_hash)
            or row.action != str(request.expected_action)
        ):
            raise ApprovalBindingError(APPROVAL_PLAN_BINDING_MISMATCH)
        if row.requester_id == str(request.actor.user_id):
            raise ApprovalActorConflictError(APPROVAL_SELF_DECISION)

        now = _database_now(self._session)
        current = _expire_approval(self._session, row, now=now)
        if current.decision is ApprovalDecision.EXPIRED:
            return current
        if current.decision is not ApprovalDecision.PENDING:
            raise ApprovalStateConflictError(APPROVAL_NOT_PENDING)
        if plan.status != PlanStatus.DRAFT.value:
            raise ApprovalStateConflictError(APPROVAL_PLAN_NOT_DRAFT)

        decided = current.model_copy(
            update={
                "decision": request.decision,
                "decided_by": request.actor,
                "decided_at": now,
                "comment": request.comment,
            }
        )
        decided = ApprovalRecord.model_validate(decided.model_dump())
        row.decision = decided.decision.value
        row.decided_by_kind = request.actor.kind.value
        row.decided_by_id = str(request.actor.user_id)
        row.decided_by_role = request.actor.role.value
        row.decided_at = decided.decided_at
        row.comment = decided.comment
        plan.status = (
            PlanStatus.APPROVED.value
            if request.decision is ApprovalDecision.APPROVED
            else PlanStatus.REJECTED.value
        )
        plan.approved_by = (
            str(request.actor.user_id) if request.decision is ApprovalDecision.APPROVED else None
        )
        self._session.flush()
        return decided


class SqlAlchemyJobRepository:
    """Use one caller-owned Session so every operation composes into one transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def enqueue(
        self,
        job: JobRecord,
        *,
        authorization: JobAuthorizationDraft,
    ) -> EnqueueJobResult:
        if job.status is not JobStatus.QUEUED:
            raise ValueError(QUEUED_REQUIRED)
        if authorization.experiment_id != job.experiment_id or authorization.plan_id != job.plan_id:
            raise IdempotencyConflictError(IDEMPOTENCY_MISMATCH)
        existing = self._session.get(
            IdempotencyRow,
            str(authorization.idempotency_key),
        )
        if existing is not None:
            return _idempotent_replay(
                self._session,
                existing,
                job=job,
                authorization=authorization,
            )

        plan = self._session.get(PlanRow, str(job.plan_id))
        if (
            plan is None
            or plan.experiment_id != str(job.experiment_id)
            or plan.kind != job.kind.value
            or plan.status != "approved"
            or plan.plan_hash != str(authorization.plan_hash)
            or plan.risk_level != authorization.risk_level.value
        ):
            raise PlanBindingError(PLAN_BINDING_MISMATCH)
        statement = (
            insert(IdempotencyRow)
            .values(
                idempotency_key=str(authorization.idempotency_key),
                schema_version="idempotency-record/v1",
                request_hash=str(authorization.request_hash),
                action=str(authorization.action),
                experiment_id=str(job.experiment_id),
                job_id=str(job.job_id),
                created_at=job.submitted_at,
            )
            .on_conflict_do_nothing(index_elements=[IdempotencyRow.idempotency_key])
            .returning(IdempotencyRow.idempotency_key)
        )
        inserted_key = self._session.execute(statement).scalar_one_or_none()
        if inserted_key is None:
            existing = self._session.get(
                IdempotencyRow,
                str(authorization.idempotency_key),
            )
            if existing is None:
                raise IdempotencyConflictError(IDEMPOTENCY_MISMATCH)
            return _idempotent_replay(
                self._session,
                existing,
                job=job,
                authorization=authorization,
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
        SqlAlchemyJobAuthorizationRepository(self._session).add(
            bind_job_authorization(
                job_id=job.job_id,
                draft=authorization,
            )
        )
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
