"""Typed persistence contracts for Job leases, idempotency, events, and audit."""

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, JsonValue, model_validator

from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.base import NonEmptyStr, StrictModel, UtcDatetime
from autopilot.domain.enums import JobStatus
from autopilot.domain.errors import ErrorEnvelope
from autopilot.domain.identifiers import (
    ArtifactId,
    AuditEventId,
    EventId,
    ExperimentId,
    JobId,
    Sha256Digest,
    ToolName,
    WorkerId,
)
from autopilot.domain.jobs import (
    TERMINAL_JOB_STATUSES,
    JobProgress,
    JobRecord,
    validate_job_transition,
)

INVALID_LEASE_TIMELINE = "Job lease timestamps must be chronological"
INVALID_EVENT_TRANSITION = "Job event states must describe an actual transition"
INVALID_EVENT_TYPE = "Job event type does not match its state"
INVALID_AUDIT_BINDING = "Job and Artifact audit references require an Experiment"
INVALID_PROVIDER_ASSIGNMENT = "provider Job ID can only be assigned when entering running"


class JobEventType(StrEnum):
    QUEUED = "job.queued"
    STATUS_CHANGED = "job.status_changed"
    STARTED = "job.started"
    PROGRESS = "job.progress"
    CANCEL_REQUESTED = "job.cancel_requested"
    COMPLETED = "job.completed"
    FAILED = "job.failed"
    CANCELLED = "job.cancelled"
    TIMED_OUT = "job.timed_out"
    LEASE_RECOVERED = "job.lease_recovered"


class AuditResult(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"


class JobLease(StrictModel):
    schema_version: Literal["job-lease/v1"] = "job-lease/v1"
    job_id: JobId
    worker_id: WorkerId
    acquired_at: UtcDatetime
    heartbeat_at: UtcDatetime
    expires_at: UtcDatetime
    fencing_token: int = Field(ge=1)
    recovered: bool

    @model_validator(mode="after")
    def validate_timeline(self) -> Self:
        if not self.acquired_at <= self.heartbeat_at < self.expires_at:
            raise ValueError(INVALID_LEASE_TIMELINE)
        return self


class ClaimedJob(StrictModel):
    schema_version: Literal["claimed-job/v1"] = "claimed-job/v1"
    job: JobRecord
    lease: JobLease


class EnqueueJobResult(StrictModel):
    schema_version: Literal["enqueue-job-result/v1"] = "enqueue-job-result/v1"
    job: JobRecord
    created: bool


class JobTransition(StrictModel):
    schema_version: Literal["job-transition/v1"] = "job-transition/v1"
    target: JobStatus
    occurred_at: UtcDatetime
    progress: JobProgress | None = None
    provider_job_id: NonEmptyStr | None = None
    result_artifact: ArtifactRef | None = None
    error: ErrorEnvelope | None = None

    @model_validator(mode="after")
    def validate_provider_assignment(self) -> Self:
        if self.provider_job_id is not None and self.target is not JobStatus.RUNNING:
            raise ValueError(INVALID_PROVIDER_ASSIGNMENT)
        return self


class IdempotencyRecord(StrictModel):
    schema_version: Literal["idempotency-record/v1"] = "idempotency-record/v1"
    idempotency_key: Sha256Digest
    request_hash: Sha256Digest
    action: ToolName
    experiment_id: ExperimentId
    job_id: JobId
    created_at: UtcDatetime


class JobEvent(StrictModel):
    schema_version: Literal["job-event/v1"] = "job-event/v1"
    event_id: EventId
    sequence: int = Field(ge=1)
    experiment_id: ExperimentId
    job_id: JobId
    event_type: JobEventType
    occurred_at: UtcDatetime
    previous_status: JobStatus | None = None
    current_status: JobStatus
    payload: dict[NonEmptyStr, JsonValue] = Field(default_factory=dict, max_length=32)

    @model_validator(mode="after")
    def validate_transition(self) -> Self:
        if self.previous_status is not None:
            try:
                validate_job_transition(self.previous_status, self.current_status)
            except ValueError as error:
                raise ValueError(INVALID_EVENT_TRANSITION) from error
            expected_type = {
                JobStatus.RUNNING: JobEventType.STARTED,
                JobStatus.SUCCEEDED: JobEventType.COMPLETED,
                JobStatus.FAILED: JobEventType.FAILED,
                JobStatus.CANCELLED: JobEventType.CANCELLED,
                JobStatus.TIMED_OUT: JobEventType.TIMED_OUT,
            }.get(self.current_status, JobEventType.STATUS_CHANGED)
            if self.event_type is not expected_type:
                raise ValueError(INVALID_EVENT_TYPE)
            return self

        valid_snapshot = (
            (self.event_type is JobEventType.QUEUED and self.current_status is JobStatus.QUEUED)
            or (
                self.event_type is JobEventType.PROGRESS
                and self.current_status is JobStatus.RUNNING
            )
            or (
                self.event_type is JobEventType.LEASE_RECOVERED
                and self.current_status not in TERMINAL_JOB_STATUSES
            )
            or (
                self.event_type is JobEventType.CANCEL_REQUESTED
                and self.current_status not in TERMINAL_JOB_STATUSES
            )
        )
        if not valid_snapshot:
            raise ValueError(INVALID_EVENT_TYPE)
        return self


class AuditEvent(StrictModel):
    schema_version: Literal["audit-event/v1"] = "audit-event/v1"
    audit_event_id: AuditEventId
    experiment_id: ExperimentId | None = None
    job_id: JobId | None = None
    actor: NonEmptyStr
    action: NonEmptyStr
    resource_type: NonEmptyStr
    resource_id: NonEmptyStr
    request_id: NonEmptyStr
    decision_id: NonEmptyStr | None = None
    before_artifact_id: ArtifactId | None = None
    after_artifact_id: ArtifactId | None = None
    result: AuditResult
    occurred_at: UtcDatetime

    @model_validator(mode="after")
    def validate_experiment_binding(self) -> Self:
        if self.experiment_id is None and (
            self.job_id is not None
            or self.before_artifact_id is not None
            or self.after_artifact_id is not None
        ):
            raise ValueError(INVALID_AUDIT_BINDING)
        return self
