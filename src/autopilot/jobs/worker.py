"""PostgreSQL Lease Worker boundary for deterministic capability execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import NoReturn, Protocol

from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.base import utc_now
from autopilot.domain.enums import ErrorCategory, JobStatus, SuggestedAction
from autopilot.domain.errors import DomainError, ErrorEnvelope
from autopilot.domain.identifiers import JobId, WorkerId
from autopilot.domain.jobs import JobProgress, JobRecord
from autopilot.gateway.models import JobAuthorizationRecord
from autopilot.jobs.models import ClaimedJob, JobTransition
from autopilot.jobs.ports import JobRepository

WORKER_AUTHORIZATION_MISSING = "WORKER_AUTHORIZATION_MISSING"
WORKER_HANDLER_MISSING = "WORKER_HANDLER_MISSING"
WORKER_EXECUTION_FAILED = "WORKER_EXECUTION_FAILED"
WORKER_CANCELLED = "WORKER_CANCELLED"
INVALID_LEASE_DURATION = "Worker lease duration must be positive"


class WorkerExecutionError(RuntimeError):
    """A redacted, classified failure at the Worker/Capability boundary."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class JobAuthorizationReader(Protocol):
    def get(self, job_id: JobId) -> JobAuthorizationRecord | None: ...


class JobPreflight(Protocol):
    """Recheck immutable authorization, Plan, budget, policy and Approval bindings."""

    def validate(self, claimed: ClaimedJob, authorization: JobAuthorizationRecord) -> None: ...


class WorkerCallbacks(Protocol):
    def update_progress(self, progress: JobProgress) -> None: ...

    def heartbeat(self) -> None: ...

    def cancellation_requested(self) -> bool: ...


class JobHandler(Protocol):
    def execute(
        self,
        claimed: ClaimedJob,
        authorization: JobAuthorizationRecord,
        callbacks: WorkerCallbacks,
    ) -> WorkerExecutionResult: ...


@dataclass(frozen=True, slots=True)
class WorkerExecutionResult:
    """Provider-neutral terminal material returned by one Capability Handler."""

    result_artifact: ArtifactRef | None = None
    provider_job_id: str | None = None
    progress: JobProgress | None = None


class _Callbacks:
    def __init__(self, worker: LeaseWorker, claimed: ClaimedJob) -> None:
        self._worker = worker
        self._claimed = claimed

    def update_progress(self, progress: JobProgress) -> None:
        self._worker._jobs.update_progress(
            job_id=self._claimed.job.job_id,
            worker_id=self._claimed.lease.worker_id,
            fencing_token=self._claimed.lease.fencing_token,
            progress=progress,
        )

    def heartbeat(self) -> None:
        self._worker._jobs.heartbeat(
            job_id=self._claimed.job.job_id,
            worker_id=self._claimed.lease.worker_id,
            fencing_token=self._claimed.lease.fencing_token,
            lease_duration=self._worker._lease_duration,
        )

    def cancellation_requested(self) -> bool:
        current = self._worker._jobs.get(self._claimed.job.job_id)
        return current is not None and current.cancel_requested_at is not None


def _error_envelope(
    code: str,
    message: str,
    *,
    retryable: bool,
    category: ErrorCategory = ErrorCategory.INFRASTRUCTURE_ERROR,
) -> ErrorEnvelope:
    return ErrorEnvelope(
        error=DomainError(
            code=code,
            category=category,
            message=message,
            retryable=retryable,
            suggested_actions=(SuggestedAction.CONTACT_OPERATOR,),
        )
    )


class LeaseWorker:
    """Run at most one leased Job and persist every lifecycle transition."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        jobs: JobRepository,
        authorizations: JobAuthorizationReader,
        preflight: JobPreflight,
        handlers: Mapping[str, JobHandler],
        lease_duration: timedelta = timedelta(seconds=60),
        worker_id: WorkerId | None = None,
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError(INVALID_LEASE_DURATION)
        self._jobs = jobs
        self._authorizations = authorizations
        self._preflight = preflight
        self._handlers = handlers
        self._lease_duration = lease_duration
        self._worker_id = worker_id or WorkerId.new()

    @property
    def worker_id(self) -> WorkerId:
        return self._worker_id

    def run_once(self) -> JobRecord | None:
        claimed = self._jobs.claim_next(
            worker_id=self._worker_id,
            lease_duration=self._lease_duration,
        )
        if claimed is None:
            return None
        return self._run_claimed(claimed)

    def _run_claimed(self, claimed: ClaimedJob) -> JobRecord:
        job = claimed.job
        if job.status is JobStatus.QUEUED:
            job = self._jobs.transition(
                job_id=job.job_id,
                transition=JobTransition(target=JobStatus.VALIDATING, occurred_at=utc_now()),
                worker_id=claimed.lease.worker_id,
                fencing_token=claimed.lease.fencing_token,
            )
            claimed = ClaimedJob(job=job, lease=claimed.lease)

        authorization = self._authorizations.get(job.job_id)
        if authorization is None:
            return self._fail(claimed, WORKER_AUTHORIZATION_MISSING, "Job authorization is missing")
        try:
            self._preflight.validate(claimed, authorization)
            if _cancel_requested(self._jobs, job.job_id):
                return self._cancel(claimed)
            handler = self._handlers.get(job.kind.value)
            if handler is None:
                _raise_missing_handler(job.kind.value)
            running = self._jobs.transition(
                job_id=job.job_id,
                transition=JobTransition(target=JobStatus.RUNNING, occurred_at=utc_now()),
                worker_id=claimed.lease.worker_id,
                fencing_token=claimed.lease.fencing_token,
            )
            claimed = ClaimedJob(job=running, lease=claimed.lease)
            result = handler.execute(claimed, authorization, _Callbacks(self, claimed))
            if _cancel_requested(self._jobs, job.job_id):
                return self._cancel(claimed)
            return self._succeed(claimed, result)
        except WorkerExecutionError as error:
            return self._fail(claimed, error.code, str(error), retryable=error.retryable)
        except (RuntimeError, ValueError) as error:
            return self._fail(claimed, WORKER_EXECUTION_FAILED, str(error))

    def _cancel(self, claimed: ClaimedJob) -> JobRecord:
        return self._jobs.transition(
            job_id=claimed.job.job_id,
            transition=JobTransition(
                target=JobStatus.CANCELLED,
                occurred_at=utc_now(),
            ),
            worker_id=claimed.lease.worker_id,
            fencing_token=claimed.lease.fencing_token,
        )

    def _succeed(self, claimed: ClaimedJob, result: WorkerExecutionResult) -> JobRecord:
        artifact = result.result_artifact
        if artifact is None:
            raise WorkerExecutionError(
                WORKER_EXECUTION_FAILED,
                "successful Worker execution did not return an Artifact",
            )
        return self._jobs.transition(
            job_id=claimed.job.job_id,
            transition=JobTransition(
                target=JobStatus.SUCCEEDED,
                occurred_at=utc_now(),
                result_artifact=artifact,
                progress=result.progress,
            ),
            worker_id=claimed.lease.worker_id,
            fencing_token=claimed.lease.fencing_token,
        )

    def _fail(
        self,
        claimed: ClaimedJob,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> JobRecord:
        return self._jobs.transition(
            job_id=claimed.job.job_id,
            transition=JobTransition(
                target=JobStatus.FAILED,
                occurred_at=utc_now(),
                error=_error_envelope(code, message, retryable=retryable),
            ),
            worker_id=claimed.lease.worker_id,
            fencing_token=claimed.lease.fencing_token,
        )


def _cancel_requested(jobs: JobRepository, job_id: JobId) -> bool:
    current = jobs.get(job_id)
    return current is not None and current.cancel_requested_at is not None


def _raise_missing_handler(kind: str) -> NoReturn:
    raise WorkerExecutionError(
        WORKER_HANDLER_MISSING,
        f"No verified handler is configured for {kind}",
    )


__all__ = [
    "JobAuthorizationReader",
    "JobHandler",
    "JobPreflight",
    "LeaseWorker",
    "WorkerCallbacks",
    "WorkerExecutionError",
    "WorkerExecutionResult",
]
