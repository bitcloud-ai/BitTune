"""Application persistence ports and small test doubles for the REST boundary."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from autopilot.domain.approvals import ApprovalRecord
from autopilot.domain.artifacts import ArtifactProducer
from autopilot.domain.base import StrictModel, utc_now
from autopilot.domain.enums import (
    ExperimentPhase,
    ExperimentStatus,
    PlanKind,
    PlanStatus,
    RiskLevel,
)
from autopilot.domain.identifiers import (
    ApprovalId,
    ArtifactId,
    DeploymentId,
    ExperimentId,
    JobId,
    PlanHash,
    PlanId,
    Sha256Digest,
    UserId,
    WorkerId,
)
from autopilot.domain.jobs import JobProgress, JobRecord
from autopilot.evidence.models import ArtifactMetadata
from autopilot.gateway.approval_ports import CreateApprovalRequest, DecideApprovalRequest
from autopilot.infrastructure.artifacts import LocalArtifactStore
from autopilot.infrastructure.database.models import (
    ArtifactRow,
    DeploymentRow,
    ExperimentRow,
    PlanRow,
)
from autopilot.infrastructure.database.repositories import (
    SqlAlchemyApprovalRepository,
    SqlAlchemyJobRepository,
)
from autopilot.jobs.models import ClaimedJob, JobEvent, JobTransition

EXPERIMENT_ALREADY_EXISTS = "Experiment already exists"


class ExperimentRecord(StrictModel):
    schema_version: str = "experiment-record/v1"
    experiment_id: ExperimentId
    created_by: UserId
    status: ExperimentStatus
    phase: ExperimentPhase
    graph_state: dict[str, JsonValue]
    created_at: datetime
    updated_at: datetime


class PlanProjection(StrictModel):
    schema_version: str = "plan-projection/v1"
    plan_id: PlanId
    experiment_id: ExperimentId
    kind: PlanKind
    schema_version_body: str
    body: dict[str, JsonValue]
    plan_hash: PlanHash
    risk_level: RiskLevel
    status: PlanStatus
    approved_by: UserId | None = None
    created_at: datetime


class DeploymentProjection(StrictModel):
    schema_version: str = "deployment-projection/v1"
    deployment_id: DeploymentId
    experiment_id: ExperimentId
    candidate_id: str
    container_id: str | None = None
    endpoint: str | None = None
    status: str
    parameter_hash: str
    image_digest: str
    model_revision: str
    gpu_id: int
    logs_artifact_id: str | None = None
    created_at: datetime
    updated_at: datetime


class ExperimentStore(Protocol):
    def create(self, record: ExperimentRecord) -> ExperimentRecord: ...

    def get(self, experiment_id: ExperimentId) -> ExperimentRecord | None: ...

    def save_state(
        self, experiment_id: ExperimentId, state: dict[str, JsonValue]
    ) -> ExperimentRecord: ...

    def cancel(self, experiment_id: ExperimentId) -> ExperimentRecord: ...


class PlanStore(Protocol):
    def get(self, experiment_id: ExperimentId, plan_id: PlanId) -> PlanProjection | None: ...


class DeploymentStore(Protocol):
    def get(
        self, experiment_id: ExperimentId, deployment_id: DeploymentId
    ) -> DeploymentProjection | None: ...


class JobStore(Protocol):
    def get(self, job_id: JobId) -> JobRecord | None: ...

    def request_cancel(self, *, job_id: JobId) -> JobRecord: ...


class ArtifactQuery(Protocol):
    def metadata(
        self, experiment_id: ExperimentId, artifact_id: str
    ) -> ArtifactMetadata | None: ...

    def read(self, experiment_id: ExperimentId, metadata: ArtifactMetadata) -> bytes: ...


class ApprovalStore(Protocol):
    def create(self, request: CreateApprovalRequest) -> ApprovalRecord: ...

    def get(self, approval_id: str) -> ApprovalRecord | None: ...

    def decide(self, request: DecideApprovalRequest) -> ApprovalRecord: ...


class InMemoryExperimentStore:
    """Test-only store; production uses :class:`SqlAlchemyExperimentStore`."""

    def __init__(self) -> None:
        self._records: dict[ExperimentId, ExperimentRecord] = {}

    def create(self, record: ExperimentRecord) -> ExperimentRecord:
        if record.experiment_id in self._records:
            raise ValueError(EXPERIMENT_ALREADY_EXISTS)
        self._records[record.experiment_id] = record
        return record

    def get(self, experiment_id: ExperimentId) -> ExperimentRecord | None:
        return self._records.get(experiment_id)

    def save_state(
        self, experiment_id: ExperimentId, state: dict[str, JsonValue]
    ) -> ExperimentRecord:
        current = self._records[experiment_id]
        updated = current.model_copy(
            update={
                "status": ExperimentStatus(str(state["status"])),
                "phase": ExperimentPhase(str(state["phase"])),
                "graph_state": state,
                "updated_at": utc_now(),
            }
        )
        self._records[experiment_id] = updated
        return updated

    def cancel(self, experiment_id: ExperimentId) -> ExperimentRecord:
        current = self._records[experiment_id]
        updated = current.model_copy(
            update={
                "status": ExperimentStatus.CANCELLED,
                "phase": ExperimentPhase.CANCELLED,
                "updated_at": utc_now(),
            }
        )
        self._records[experiment_id] = updated
        return updated


class SqlAlchemyExperimentStore:
    """Persist Graph State and the Experiment projection in PostgreSQL."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @staticmethod
    def _record(row: ExperimentRow) -> ExperimentRecord:
        return ExperimentRecord(
            experiment_id=ExperimentId(root=row.id),
            created_by=UserId(root=row.created_by),
            status=ExperimentStatus(row.status),
            phase=ExperimentPhase(row.phase),
            graph_state=row.graph_state_json,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def create(self, record: ExperimentRecord) -> ExperimentRecord:
        with self._sessions.begin() as session:
            session.add(
                ExperimentRow(
                    id=str(record.experiment_id),
                    status=record.status.value,
                    phase=record.phase.value,
                    created_by=str(record.created_by),
                    graph_state_json=record.graph_state,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                )
            )
        return record

    def get(self, experiment_id: ExperimentId) -> ExperimentRecord | None:
        with self._sessions() as session:
            row = session.get(ExperimentRow, str(experiment_id))
            return self._record(row) if row is not None else None

    def save_state(
        self, experiment_id: ExperimentId, state: dict[str, JsonValue]
    ) -> ExperimentRecord:
        with self._sessions.begin() as session:
            row = session.get(ExperimentRow, str(experiment_id), with_for_update=True)
            if row is None:
                raise KeyError(str(experiment_id))
            row.status = str(state["status"])
            row.phase = str(state["phase"])
            row.graph_state_json = state
            row.updated_at = utc_now()
            session.flush()
            return self._record(row)

    def cancel(self, experiment_id: ExperimentId) -> ExperimentRecord:
        with self._sessions.begin() as session:
            row = session.get(ExperimentRow, str(experiment_id), with_for_update=True)
            if row is None:
                raise KeyError(str(experiment_id))
            row.status = ExperimentStatus.CANCELLED.value
            row.phase = ExperimentPhase.CANCELLED.value
            row.updated_at = utc_now()
            session.flush()
            return self._record(row)


class SqlAlchemyJobStore:
    """Open one transaction per REST Job operation over the shared PostgreSQL queue."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def get(self, job_id: JobId) -> JobRecord | None:
        with self._sessions() as session:
            return SqlAlchemyJobRepository(session).get(job_id)

    def claim_next(self, *, worker_id: WorkerId, lease_duration: timedelta) -> ClaimedJob | None:
        with self._sessions.begin() as session:
            return SqlAlchemyJobRepository(session).claim_next(
                worker_id=worker_id,
                lease_duration=lease_duration,
            )

    def heartbeat(
        self,
        *,
        job_id: JobId,
        worker_id: WorkerId,
        fencing_token: int,
        lease_duration: timedelta,
    ) -> ClaimedJob:
        with self._sessions.begin() as session:
            return SqlAlchemyJobRepository(session).heartbeat(
                job_id=job_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
                lease_duration=lease_duration,
            )

    def transition(
        self,
        *,
        job_id: JobId,
        transition: JobTransition,
        worker_id: WorkerId | None = None,
        fencing_token: int | None = None,
    ) -> JobRecord:
        with self._sessions.begin() as session:
            return SqlAlchemyJobRepository(session).transition(
                job_id=job_id,
                transition=transition,
                worker_id=worker_id,
                fencing_token=fencing_token,
            )

    def update_progress(
        self,
        *,
        job_id: JobId,
        worker_id: WorkerId,
        fencing_token: int,
        progress: JobProgress,
    ) -> JobRecord:
        with self._sessions.begin() as session:
            return SqlAlchemyJobRepository(session).update_progress(
                job_id=job_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
                progress=progress,
            )

    def request_cancel(self, *, job_id: JobId) -> JobRecord:
        with self._sessions.begin() as session:
            return SqlAlchemyJobRepository(session).request_cancel(job_id=job_id)

    def list_events(self, job_id: JobId) -> tuple[JobEvent, ...]:
        with self._sessions() as session:
            return SqlAlchemyJobRepository(session).list_events(job_id)


class SqlAlchemyPlanStore:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def get(self, experiment_id: ExperimentId, plan_id: PlanId) -> PlanProjection | None:
        with self._sessions() as session:
            row = session.scalar(
                select(PlanRow).where(
                    PlanRow.experiment_id == str(experiment_id),
                    PlanRow.id == str(plan_id),
                )
            )
            if row is None:
                return None
            return PlanProjection(
                plan_id=PlanId(root=row.id),
                experiment_id=ExperimentId(root=row.experiment_id),
                kind=PlanKind(row.kind),
                schema_version_body=row.schema_version,
                body=row.body_json,
                plan_hash=PlanHash(root=row.plan_hash),
                risk_level=RiskLevel(row.risk_level),
                status=PlanStatus(row.status),
                approved_by=UserId(root=row.approved_by) if row.approved_by else None,
                created_at=row.created_at,
            )


class SqlAlchemyApprovalStore:
    """Own transaction boundaries for API approval requests and decisions."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def create(self, request: CreateApprovalRequest) -> ApprovalRecord:
        with self._sessions.begin() as session:
            return SqlAlchemyApprovalRepository(session).create(request)

    def get(self, approval_id: str) -> ApprovalRecord | None:
        with self._sessions.begin() as session:
            return SqlAlchemyApprovalRepository(session).get(ApprovalId(root=approval_id))

    def decide(self, request: DecideApprovalRequest) -> ApprovalRecord:
        with self._sessions.begin() as session:
            return SqlAlchemyApprovalRepository(session).decide(request)


class SqlAlchemyDeploymentStore:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def get(
        self, experiment_id: ExperimentId, deployment_id: DeploymentId
    ) -> DeploymentProjection | None:
        with self._sessions() as session:
            row = session.scalar(
                select(DeploymentRow).where(
                    DeploymentRow.experiment_id == str(experiment_id),
                    DeploymentRow.id == str(deployment_id),
                )
            )
            if row is None:
                return None
            return DeploymentProjection(
                deployment_id=DeploymentId(root=row.id),
                experiment_id=ExperimentId(root=row.experiment_id),
                candidate_id=row.candidate_id,
                container_id=row.container_id,
                endpoint=row.endpoint,
                status=row.status,
                parameter_hash=row.parameter_hash,
                image_digest=row.image_digest,
                model_revision=row.model_revision,
                gpu_id=row.gpu_id,
                logs_artifact_id=row.logs_artifact_id,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )


class LocalArtifactQuery:
    def __init__(self, store: LocalArtifactStore) -> None:
        self._store = store

    def metadata(self, experiment_id: ExperimentId, artifact_id: str) -> ArtifactMetadata | None:
        try:
            return self._store.get_metadata(
                experiment_id=experiment_id,
                category="evidence",
                artifact_id=ArtifactId(root=artifact_id),
            )
        except (ValueError, RuntimeError):
            return None

    def read(self, experiment_id: ExperimentId, metadata: ArtifactMetadata) -> bytes:
        return self._store.read(
            experiment_id=experiment_id,
            category=metadata.category,
            artifact_id=metadata.artifact_id,
        )


class SqlAlchemyArtifactQuery:
    """Resolve an Artifact ID through PostgreSQL before reading the root-confined store."""

    def __init__(self, sessions: sessionmaker[Session], store: LocalArtifactStore) -> None:
        self._sessions = sessions
        self._store = store

    def metadata(self, experiment_id: ExperimentId, artifact_id: str) -> ArtifactMetadata | None:
        with self._sessions() as session:
            row = session.scalar(
                select(ArtifactRow).where(
                    ArtifactRow.experiment_id == str(experiment_id),
                    ArtifactRow.id == artifact_id,
                )
            )
        if row is None:
            return None
        return ArtifactMetadata(
            artifact_id=ArtifactId(root=row.id),
            experiment_id=ExperimentId(root=row.experiment_id),
            category=row.category,
            content_type=row.content_type,
            size_bytes=row.size_bytes,
            sha256=Sha256Digest(root=row.sha256),
            created_at=row.created_at,
            producer=ArtifactProducer(
                component=row.producer_component,
                version=row.producer_version,
            ),
            storage_path=row.storage_path,
        )

    def read(self, experiment_id: ExperimentId, metadata: ArtifactMetadata) -> bytes:
        return self._store.read(
            experiment_id=experiment_id,
            category=metadata.category,
            artifact_id=metadata.artifact_id,
        )


__all__ = [
    "ApprovalStore",
    "ArtifactQuery",
    "DeploymentProjection",
    "DeploymentStore",
    "ExperimentRecord",
    "ExperimentStore",
    "InMemoryExperimentStore",
    "JobStore",
    "LocalArtifactQuery",
    "PlanProjection",
    "PlanStore",
    "SqlAlchemyApprovalStore",
    "SqlAlchemyArtifactQuery",
    "SqlAlchemyDeploymentStore",
    "SqlAlchemyExperimentStore",
    "SqlAlchemyPlanStore",
]
