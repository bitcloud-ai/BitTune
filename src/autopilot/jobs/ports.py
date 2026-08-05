"""Application-facing persistence ports for asynchronous Jobs and audit records."""

from datetime import timedelta
from typing import Protocol

from autopilot.domain.identifiers import JobId, Sha256Digest, ToolName, WorkerId
from autopilot.domain.jobs import JobProgress, JobRecord
from autopilot.jobs.models import (
    AuditEvent,
    ClaimedJob,
    EnqueueJobResult,
    JobEvent,
    JobTransition,
)


class JobRepository(Protocol):
    def enqueue(
        self,
        job: JobRecord,
        *,
        idempotency_key: Sha256Digest,
        request_hash: Sha256Digest,
        action: ToolName,
    ) -> EnqueueJobResult: ...

    def get(self, job_id: JobId) -> JobRecord | None: ...

    def claim_next(
        self,
        *,
        worker_id: WorkerId,
        lease_duration: timedelta,
    ) -> ClaimedJob | None: ...

    def heartbeat(
        self,
        *,
        job_id: JobId,
        worker_id: WorkerId,
        fencing_token: int,
        lease_duration: timedelta,
    ) -> ClaimedJob: ...

    def transition(
        self,
        *,
        job_id: JobId,
        transition: JobTransition,
        worker_id: WorkerId | None = None,
        fencing_token: int | None = None,
    ) -> JobRecord: ...

    def update_progress(
        self,
        *,
        job_id: JobId,
        worker_id: WorkerId,
        fencing_token: int,
        progress: JobProgress,
    ) -> JobRecord: ...

    def request_cancel(self, *, job_id: JobId) -> JobRecord: ...

    def list_events(self, job_id: JobId) -> tuple[JobEvent, ...]: ...


class AuditRepository(Protocol):
    def append(self, event: AuditEvent) -> None: ...
