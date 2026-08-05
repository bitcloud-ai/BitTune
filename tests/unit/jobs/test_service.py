import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from autopilot.domain.artifacts import ArtifactProducer, ArtifactRef
from autopilot.domain.enums import JobKind, JobStatus
from autopilot.domain.identifiers import (
    ArtifactId,
    AuditEventId,
    EventId,
    ExperimentId,
    JobId,
    PlanId,
    Sha256Digest,
    WorkerId,
)
from autopilot.domain.jobs import JobRecord
from autopilot.jobs.models import (
    AuditEvent,
    AuditResult,
    JobEvent,
    JobEventType,
    JobLease,
    JobTransition,
)
from autopilot.jobs.service import transition_job


def queued_job(now: datetime) -> JobRecord:
    return JobRecord(
        job_id=JobId.new(),
        experiment_id=ExperimentId.new(),
        plan_id=PlanId.new(),
        kind=JobKind.BENCHMARK,
        status=JobStatus.QUEUED,
        submitted_at=now,
    )


@pytest.fixture
def artifact_ref() -> ArtifactRef:
    raw = b"job-result"
    return ArtifactRef(
        artifact_id=ArtifactId.new(),
        sha256=Sha256Digest(root=f"sha256:{hashlib.sha256(raw).hexdigest()}"),
        content_type="application/json",
        size_bytes=len(raw),
        producer=ArtifactProducer(component="unit-test", version="1.0.0"),
    )


def test_transition_job_builds_validating_running_and_succeeded_snapshots(artifact_ref) -> None:
    now = datetime.now(UTC)
    queued = queued_job(now)

    validating = transition_job(
        queued,
        JobTransition(target=JobStatus.VALIDATING, occurred_at=now + timedelta(seconds=1)),
    )
    running = transition_job(
        validating,
        JobTransition(
            target=JobStatus.RUNNING,
            occurred_at=now + timedelta(seconds=2),
            provider_job_id="provider-job-1",
        ),
    )
    succeeded = transition_job(
        running,
        JobTransition(
            target=JobStatus.SUCCEEDED,
            occurred_at=now + timedelta(seconds=3),
            result_artifact=artifact_ref,
        ),
    )

    assert validating.started_at is None
    assert running.started_at == now + timedelta(seconds=2)
    assert running.provider_job_id == "provider-job-1"
    assert succeeded.ended_at == now + timedelta(seconds=3)
    assert succeeded.provider_job_id == "provider-job-1"
    assert succeeded.result_artifact == artifact_ref


def test_transition_job_rejects_missing_terminal_data() -> None:
    now = datetime.now(UTC)
    validating = transition_job(
        queued_job(now),
        JobTransition(target=JobStatus.VALIDATING, occurred_at=now + timedelta(seconds=1)),
    )
    running = transition_job(
        validating,
        JobTransition(target=JobStatus.RUNNING, occurred_at=now + timedelta(seconds=2)),
    )

    with pytest.raises(ValidationError, match="terminal data"):
        transition_job(
            running,
            JobTransition(target=JobStatus.SUCCEEDED, occurred_at=now + timedelta(seconds=3)),
        )


def test_job_lease_rejects_expired_or_reverse_timeline() -> None:
    now = datetime.now(UTC)

    with pytest.raises(ValidationError, match="chronological"):
        JobLease(
            job_id=JobId.new(),
            worker_id=WorkerId.new(),
            acquired_at=now,
            heartbeat_at=now + timedelta(seconds=2),
            expires_at=now + timedelta(seconds=1),
            fencing_token=1,
            recovered=False,
        )


def test_job_event_rejects_fake_state_transition() -> None:
    now = datetime.now(UTC)

    with pytest.raises(ValidationError, match="actual transition"):
        JobEvent(
            event_id=EventId.new(),
            sequence=1,
            experiment_id=ExperimentId.new(),
            job_id=JobId.new(),
            event_type=JobEventType.STATUS_CHANGED,
            occurred_at=now,
            previous_status=JobStatus.QUEUED,
            current_status=JobStatus.QUEUED,
        )


def test_job_event_rejects_illegal_jump_and_mismatched_type() -> None:
    now = datetime.now(UTC)

    with pytest.raises(ValidationError, match="actual transition"):
        JobEvent(
            event_id=EventId.new(),
            sequence=1,
            experiment_id=ExperimentId.new(),
            job_id=JobId.new(),
            event_type=JobEventType.COMPLETED,
            occurred_at=now,
            previous_status=JobStatus.QUEUED,
            current_status=JobStatus.SUCCEEDED,
        )

    with pytest.raises(ValidationError, match="event type"):
        JobEvent(
            event_id=EventId.new(),
            sequence=1,
            experiment_id=ExperimentId.new(),
            job_id=JobId.new(),
            event_type=JobEventType.PROGRESS,
            occurred_at=now,
            previous_status=JobStatus.QUEUED,
            current_status=JobStatus.VALIDATING,
        )


def test_transition_rejects_provider_id_before_running() -> None:
    with pytest.raises(ValidationError, match="provider Job ID"):
        JobTransition(
            target=JobStatus.VALIDATING,
            occurred_at=datetime.now(UTC),
            provider_job_id="provider-job-1",
        )


def test_audit_job_reference_requires_experiment_binding() -> None:
    with pytest.raises(ValidationError, match="require an Experiment"):
        AuditEvent(
            audit_event_id=AuditEventId.new(),
            job_id=JobId.new(),
            actor="operator-1",
            action="request_cancel",
            resource_type="job",
            resource_id="job-1",
            request_id="request-1",
            result=AuditResult.SUCCEEDED,
            occurred_at=datetime.now(UTC),
        )
