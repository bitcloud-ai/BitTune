from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from autopilot.domain.artifacts import ArtifactProducer, ArtifactRef
from autopilot.domain.enums import JobKind, JobStatus, RiskLevel, UserRole
from autopilot.domain.identifiers import (
    ArtifactId,
    ExperimentId,
    JobId,
    PlanHash,
    PlanId,
    Sha256Digest,
    ToolName,
    ToolSetId,
    UserId,
    WorkerId,
)
from autopilot.domain.identities import HumanSubject
from autopilot.domain.jobs import JobProgress, JobRecord
from autopilot.gateway.models import JobAuthorizationRecord
from autopilot.jobs.models import ClaimedJob, JobLease, JobTransition
from autopilot.jobs.service import transition_job
from autopilot.jobs.worker import (
    LeaseWorker,
    WorkerCallbacks,
    WorkerExecutionResult,
)

NOW = datetime(2026, 8, 6, tzinfo=UTC)


def _artifact() -> ArtifactRef:
    raw = b"worker-result"
    return ArtifactRef(
        artifact_id=ArtifactId.new(),
        sha256=Sha256Digest(root=f"sha256:{hashlib.sha256(raw).hexdigest()}"),
        content_type="application/json",
        size_bytes=len(raw),
        producer=ArtifactProducer(component="worker-test", version="1.0.0"),
    )


class _Jobs:
    def __init__(self, job: JobRecord) -> None:
        self.job = job
        self.lease: JobLease | None = None
        self.cancelled = False

    def enqueue(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    def claim_next(self, *, worker_id: WorkerId, lease_duration: Any) -> ClaimedJob | None:
        if self.lease is not None or self.job.status in {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.TIMED_OUT,
        }:
            return None
        self.lease = JobLease(
            job_id=self.job.job_id,
            worker_id=worker_id,
            acquired_at=NOW,
            heartbeat_at=NOW,
            expires_at=NOW + lease_duration,
            fencing_token=1,
            recovered=False,
        )
        return ClaimedJob(job=self.job, lease=self.lease)

    def heartbeat(self, **kwargs: Any) -> ClaimedJob:  # noqa: ARG002
        return ClaimedJob(job=self.job, lease=self.lease)

    def transition(
        self,
        *,
        job_id: JobId,
        transition: JobTransition,
        **kwargs: Any,
    ) -> JobRecord:  # noqa: ARG002
        self.job = transition_job(self.job, transition)
        if self.job.status in {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.TIMED_OUT,
        }:
            self.lease = None
        return self.job

    def update_progress(self, *, progress: JobProgress, **kwargs: Any) -> JobRecord:  # noqa: ARG002
        self.job = self.job.model_copy(update={"progress": progress})
        return self.job

    def request_cancel(self, *, job_id: JobId) -> JobRecord:  # noqa: ARG002
        self.cancelled = True
        self.job = self.job.model_copy(update={"cancel_requested_at": NOW})
        return self.job

    def get(self, job_id: JobId) -> JobRecord:  # noqa: ARG002
        return self.job

    def list_events(self, job_id: JobId) -> tuple[Any, ...]:  # noqa: ARG002
        return ()


class _Authorizations:
    def __init__(self, record: JobAuthorizationRecord) -> None:
        self.record = record

    def get(self, job_id: JobId) -> JobAuthorizationRecord | None:
        return self.record if job_id == self.record.job_id else None


class _Preflight:
    def __init__(self) -> None:
        self.calls = 0

    def validate(self, claimed: ClaimedJob, authorization: JobAuthorizationRecord) -> None:
        self.calls += 1
        assert claimed.job.job_id == authorization.job_id


class _Handler:
    def __init__(self, *, artifact: ArtifactRef, cancel: bool = False) -> None:
        self.artifact = artifact
        self.cancel = cancel
        self.calls = 0

    def execute(
        self,
        claimed: ClaimedJob,
        authorization: JobAuthorizationRecord,
        callbacks: WorkerCallbacks,
    ) -> WorkerExecutionResult:  # noqa: ARG002
        self.calls += 1
        if self.cancel:
            callbacks.update_progress(
                JobProgress(
                    stage="benchmark",
                    completed_units=1,
                    total_units=1,
                    latest_message="cancel",
                )
            )
        return WorkerExecutionResult(result_artifact=self.artifact)


def _job() -> JobRecord:
    return JobRecord(
        job_id=JobId.new(),
        experiment_id=ExperimentId.new(),
        plan_id=PlanId.new(),
        kind=JobKind.BENCHMARK,
        status=JobStatus.QUEUED,
        submitted_at=NOW,
    )


def _authorization(job: JobRecord) -> JobAuthorizationRecord:
    return JobAuthorizationRecord(
        job_id=job.job_id,
        experiment_id=job.experiment_id,
        subject=HumanSubject(user_id=UserId.new(), role=UserRole.OPERATOR),
        action=ToolName(root="start_benchmark"),
        risk_level=RiskLevel.L1,
        plan_id=job.plan_id,
        plan_hash=PlanHash(root="sha256:" + "a" * 64),
        tool_schema_version="plan-execution-request/v1",
        tool_set_id=ToolSetId.new(),
        tool_set_version=Sha256Digest(root="sha256:" + "b" * 64),
        policy_decision_id="decision-worker-test",
        request_hash=Sha256Digest(root="sha256:" + "c" * 64),
        idempotency_key=Sha256Digest(root="sha256:" + "d" * 64),
        authorized_at=NOW,
    )


def test_worker_runs_queued_job_to_succeeded() -> None:
    job = _job()
    jobs = _Jobs(job)
    preflight = _Preflight()
    handler = _Handler(artifact=_artifact())
    result = LeaseWorker(
        jobs=jobs,
        authorizations=_Authorizations(_authorization(job)),
        preflight=preflight,
        handlers={"benchmark": handler},
        worker_id=WorkerId.new(),
    ).run_once()

    assert result is not None
    assert result.status is JobStatus.SUCCEEDED
    assert preflight.calls == 1
    assert handler.calls == 1


def test_worker_fails_closed_without_verified_handler() -> None:
    job = _job()
    result = LeaseWorker(
        jobs=_Jobs(job),
        authorizations=_Authorizations(_authorization(job)),
        preflight=_Preflight(),
        handlers={},
    ).run_once()

    assert result is not None
    assert result.status is JobStatus.FAILED
    assert result.error is not None
    assert result.error.error.code == "WORKER_HANDLER_MISSING"


def test_worker_cancels_before_handler_when_request_is_persisted() -> None:
    job = _job()
    jobs = _Jobs(job)
    jobs.request_cancel(job_id=job.job_id)
    handler = _Handler(artifact=_artifact())
    result = LeaseWorker(
        jobs=jobs,
        authorizations=_Authorizations(_authorization(job)),
        preflight=_Preflight(),
        handlers={"benchmark": handler},
    ).run_once()

    assert result is not None
    assert result.status is JobStatus.CANCELLED
    assert handler.calls == 0
