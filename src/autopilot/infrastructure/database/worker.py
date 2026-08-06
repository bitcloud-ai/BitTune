"""PostgreSQL authorization rechecks used by the Lease Worker."""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from autopilot.domain.enums import RiskLevel
from autopilot.domain.identifiers import JobId
from autopilot.gateway.models import JobAuthorizationRecord
from autopilot.infrastructure.database.gateway_repositories import (
    SqlAlchemyJobAuthorizationRepository,
    SqlAlchemyPlanAuthorizationRepository,
)
from autopilot.infrastructure.database.repositories import SqlAlchemyApprovalRepository
from autopilot.jobs.models import ClaimedJob
from autopilot.jobs.worker import JobAuthorizationReader, JobPreflight, WorkerExecutionError

WORKER_AUTHORIZATION_MISMATCH = "WORKER_AUTHORIZATION_MISMATCH"
WORKER_PLAN_RECHECK_FAILED = "WORKER_PLAN_RECHECK_FAILED"
WORKER_APPROVAL_RECHECK_FAILED = "WORKER_APPROVAL_RECHECK_FAILED"
WORKER_POLICY_RECHECK_FAILED = "WORKER_POLICY_RECHECK_FAILED"


class WorkerPolicyRechecker(Protocol):
    """Re-evaluate OPA with deployment-trusted hardware/provider context."""

    def validate(self, claimed: ClaimedJob, authorization: JobAuthorizationRecord) -> None: ...


class RejectingWorkerPolicyRechecker:
    """Default policy boundary; production must provide an OPA-backed implementation."""

    def validate(self, claimed: ClaimedJob, authorization: JobAuthorizationRecord) -> None:
        del claimed, authorization
        raise WorkerExecutionError(
            WORKER_POLICY_RECHECK_FAILED,
            "Worker OPA recheck is not configured",
        )


class SqlAlchemyJobAuthorizationReader(JobAuthorizationReader):
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def get(self, job_id: JobId) -> JobAuthorizationRecord | None:
        with self._sessions() as session:
            return SqlAlchemyJobAuthorizationRepository(session).get(job_id)


class SqlAlchemyWorkerPreflight(JobPreflight):
    """Recheck persisted authorization material immediately before Provider execution."""

    def __init__(
        self,
        *,
        sessions: sessionmaker[Session],
        policy: WorkerPolicyRechecker,
    ) -> None:
        self._sessions = sessions
        self._policy = policy

    def validate(self, claimed: ClaimedJob, authorization: JobAuthorizationRecord) -> None:
        job = claimed.job
        if (
            authorization.job_id != job.job_id
            or authorization.experiment_id != job.experiment_id
            or authorization.plan_id != job.plan_id
        ):
            raise WorkerExecutionError(
                WORKER_AUTHORIZATION_MISMATCH,
                "Job Authorization does not match the leased Job",
            )
        with self._sessions.begin() as session:
            try:
                plan = SqlAlchemyPlanAuthorizationRepository(session).get_for_execution(
                    experiment_id=job.experiment_id,
                    plan_id=job.plan_id,
                    expected_plan_hash=authorization.plan_hash,
                )
            except (RuntimeError, ValueError) as error:
                raise WorkerExecutionError(
                    WORKER_PLAN_RECHECK_FAILED,
                    "persisted Plan failed the Worker recheck",
                ) from error
            if plan.risk_level is not authorization.risk_level:
                raise WorkerExecutionError(
                    WORKER_PLAN_RECHECK_FAILED,
                    "Job Authorization risk does not match the persisted Plan",
                )
            if authorization.risk_level is RiskLevel.L2:
                if authorization.approval_id is None:
                    raise WorkerExecutionError(
                        WORKER_APPROVAL_RECHECK_FAILED,
                        "L2 Job Authorization has no Approval binding",
                    )
                try:
                    SqlAlchemyApprovalRepository(session).require_valid_for_execution(
                        approval_id=authorization.approval_id,
                        experiment_id=job.experiment_id,
                        plan_id=job.plan_id,
                        expected_plan_hash=authorization.plan_hash,
                        action=authorization.action,
                    )
                except (RuntimeError, ValueError) as error:
                    raise WorkerExecutionError(
                        WORKER_APPROVAL_RECHECK_FAILED,
                        "persisted Approval failed the Worker recheck",
                    ) from error
        self._policy.validate(claimed, authorization)


__all__ = [
    "RejectingWorkerPolicyRechecker",
    "SqlAlchemyJobAuthorizationReader",
    "SqlAlchemyWorkerPreflight",
    "WorkerPolicyRechecker",
]
