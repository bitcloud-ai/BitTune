"""Persistent asynchronous Job contract and deterministic transitions."""

from typing import Literal, Self

from pydantic import Field, model_validator

from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.base import LongText, StrictModel, UtcDatetime
from autopilot.domain.enums import JobKind, JobStatus
from autopilot.domain.errors import ErrorEnvelope
from autopilot.domain.identifiers import ExperimentId, JobId, PlanId

INVALID_PROGRESS = "completed progress units cannot exceed total units"
INVALID_JOB_TIMESTAMPS = "job timestamps do not match the current status"
INVALID_JOB_TIMELINE = "job timestamps are not chronological"
INVALID_JOB_RESULT = "job terminal data does not match the current status"
INVALID_JOB_TRANSITION = "job status transition is not allowed"

TERMINAL_JOB_STATUSES = frozenset(
    {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.TIMED_OUT}
)
JOB_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.VALIDATING, JobStatus.CANCELLED}),
    JobStatus.VALIDATING: frozenset(
        {
            JobStatus.WAITING_APPROVAL,
            JobStatus.RUNNING,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.WAITING_APPROVAL: frozenset(
        {JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.TIMED_OUT}
    ),
    JobStatus.RUNNING: TERMINAL_JOB_STATUSES,
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
    JobStatus.TIMED_OUT: frozenset(),
}


class JobProgress(StrictModel):
    stage: LongText
    completed_units: int = Field(ge=0, le=1_000_000)
    total_units: int = Field(ge=1, le=1_000_000)
    latest_message: LongText

    @model_validator(mode="after")
    def validate_units(self) -> Self:
        if self.completed_units > self.total_units:
            raise ValueError(INVALID_PROGRESS)
        return self


class JobRecord(StrictModel):
    schema_version: Literal["job/v1"] = "job/v1"
    job_id: JobId
    experiment_id: ExperimentId
    plan_id: PlanId
    kind: JobKind
    status: JobStatus
    progress: JobProgress | None = None
    submitted_at: UtcDatetime
    started_at: UtcDatetime | None = None
    ended_at: UtcDatetime | None = None
    result_artifact: ArtifactRef | None = None
    error: ErrorEnvelope | None = None

    @model_validator(mode="after")
    def validate_state_data(self) -> Self:
        if self.status in {JobStatus.RUNNING, JobStatus.SUCCEEDED} and self.started_at is None:
            raise ValueError(INVALID_JOB_TIMESTAMPS)
        if self.status in TERMINAL_JOB_STATUSES and self.ended_at is None:
            raise ValueError(INVALID_JOB_TIMESTAMPS)
        if self.status not in TERMINAL_JOB_STATUSES and self.ended_at is not None:
            raise ValueError(INVALID_JOB_TIMESTAMPS)
        if (
            self.status
            in {
                JobStatus.QUEUED,
                JobStatus.VALIDATING,
                JobStatus.WAITING_APPROVAL,
            }
            and self.started_at is not None
        ):
            raise ValueError(INVALID_JOB_TIMESTAMPS)
        if self.started_at is not None and self.started_at < self.submitted_at:
            raise ValueError(INVALID_JOB_TIMELINE)
        timeline_start = self.started_at or self.submitted_at
        if self.ended_at is not None and self.ended_at < timeline_start:
            raise ValueError(INVALID_JOB_TIMELINE)
        if self.status is JobStatus.SUCCEEDED:
            if self.result_artifact is None or self.error is not None:
                raise ValueError(INVALID_JOB_RESULT)
        elif self.result_artifact is not None:
            raise ValueError(INVALID_JOB_RESULT)
        error_required = self.status in {JobStatus.FAILED, JobStatus.TIMED_OUT}
        if error_required != (self.error is not None):
            raise ValueError(INVALID_JOB_RESULT)
        return self


def validate_job_transition(current: JobStatus, target: JobStatus) -> None:
    """Reject state transitions outside the persisted Job state machine."""
    if target not in JOB_TRANSITIONS[current]:
        raise ValueError(INVALID_JOB_TRANSITION)
