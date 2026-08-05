"""Pure Job state transitions shared by persistence adapters."""

from autopilot.domain.enums import JobStatus
from autopilot.domain.jobs import (
    TERMINAL_JOB_STATUSES,
    JobRecord,
    validate_job_transition,
)
from autopilot.jobs.models import JobTransition


def transition_job(
    job: JobRecord,
    transition: JobTransition,
) -> JobRecord:
    """Build and validate the next immutable Job snapshot."""
    target = transition.target
    validate_job_transition(job.status, target)
    payload = job.model_dump()
    payload["status"] = target
    if transition.progress is not None:
        payload["progress"] = transition.progress
    if transition.provider_job_id is not None:
        payload["provider_job_id"] = transition.provider_job_id
    if target is JobStatus.RUNNING and job.started_at is None:
        payload["started_at"] = transition.occurred_at
    if target in TERMINAL_JOB_STATUSES:
        payload["ended_at"] = transition.occurred_at
    payload["result_artifact"] = transition.result_artifact
    payload["error"] = transition.error
    return JobRecord.model_validate(payload)
