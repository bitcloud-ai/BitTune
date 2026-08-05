"""PostgreSQL repositories for M4 Gateway authorization evidence."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from autopilot.domain.base import SchemaVersion
from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.enums import ExperimentPhase, PlanStatus, RiskLevel, UserRole
from autopilot.domain.identifiers import (
    ExperimentId,
    JobId,
    PlanHash,
    PlanId,
    ToolSetId,
    UserId,
)
from autopilot.domain.identities import HumanSubject, ServiceSubject, Subject, SubjectKind
from autopilot.gateway.errors import IdempotencyAuthorizationError, PlanAuthorizationError
from autopilot.gateway.models import (
    JobAuthorizationDraft,
    JobAuthorizationRecord,
    JobIdempotencyClaim,
    PlanAuthorizationMaterial,
    ToolSetEntry,
    ToolSetSnapshot,
)
from autopilot.infrastructure.database.errors import AuthorizationBindingError
from autopilot.infrastructure.database.models import (
    IdempotencyRow,
    JobAuthorizationRow,
    JobRow,
    PlanRow,
    ToolSetSnapshotRow,
)

AUTHORIZATION_BINDING_MISMATCH = "authorization evidence conflicts with persisted material"
IDEMPOTENCY_BINDING_MISMATCH = "idempotency key conflicts with persisted request material"
PLAN_AUTHORIZATION_INVALID = "persisted Plan is missing or cannot authorize execution"


class _ExecutionSpecificationProjection(BaseModel):
    """Strictly read the authorization fields from a provider-specific specification."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    schema_version: SchemaVersion
    budget: ExecutionBudget


class _PlanBodyProjection(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    execution_specification: _ExecutionSpecificationProjection


def _subject_columns(subject: Subject) -> tuple[str, str, str | None]:
    if isinstance(subject, HumanSubject):
        return subject.kind.value, str(subject.user_id), subject.role.value
    return subject.kind.value, subject.service_name, None


def _subject_from_columns(*, kind: str, subject_id: str, role: str | None) -> Subject:
    try:
        subject_kind = SubjectKind(kind)
    except ValueError as error:
        raise AuthorizationBindingError(AUTHORIZATION_BINDING_MISMATCH) from error
    if subject_kind is SubjectKind.HUMAN:
        if role is None:
            raise AuthorizationBindingError(AUTHORIZATION_BINDING_MISMATCH)
        try:
            return HumanSubject(
                user_id=UserId(root=subject_id),
                role=UserRole(role),
            )
        except (ValidationError, ValueError) as error:
            raise AuthorizationBindingError(AUTHORIZATION_BINDING_MISMATCH) from error
    if role is not None:
        raise AuthorizationBindingError(AUTHORIZATION_BINDING_MISMATCH)
    try:
        return ServiceSubject(service_name=subject_id)
    except (ValidationError, ValueError) as error:
        raise AuthorizationBindingError(AUTHORIZATION_BINDING_MISMATCH) from error


def _string_tuple(values: list[JsonValue]) -> tuple[str, ...]:
    if any(not isinstance(value, str) for value in values):
        raise AuthorizationBindingError(AUTHORIZATION_BINDING_MISMATCH)
    return tuple(value for value in values if isinstance(value, str))


def _toolset_snapshot(row: ToolSetSnapshotRow) -> ToolSetSnapshot:
    try:
        return ToolSetSnapshot(
            schema_version=row.schema_version,
            tool_set_id=ToolSetId(root=row.id),
            tool_set_version=row.tool_set_version,
            experiment_id=ExperimentId(root=row.experiment_id),
            subject=_subject_from_columns(
                kind=row.subject_kind,
                subject_id=row.subject_id,
                role=row.subject_role,
            ),
            phase=ExperimentPhase(row.phase),
            hardware_capabilities=_string_tuple(row.hardware_capabilities_json),
            enabled_providers=_string_tuple(row.enabled_providers_json),
            enabled_feature_flags=_string_tuple(row.enabled_feature_flags_json),
            tools=tuple(ToolSetEntry.model_validate(value) for value in row.tools_json),
            policy_decision_ids=_string_tuple(row.policy_decision_ids_json),
            created_at=row.created_at,
        )
    except (ValidationError, ValueError) as error:
        raise AuthorizationBindingError(AUTHORIZATION_BINDING_MISMATCH) from error


class SqlAlchemyToolSetSnapshotRepository:
    """Persist the exact Tool Set sent with an LLM request as an immutable snapshot."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, snapshot: ToolSetSnapshot) -> None:
        existing = self._session.get(ToolSetSnapshotRow, str(snapshot.tool_set_id))
        if existing is not None:
            if _toolset_snapshot(existing) != snapshot:
                raise AuthorizationBindingError(AUTHORIZATION_BINDING_MISMATCH)
            return
        subject_kind, subject_id, subject_role = _subject_columns(snapshot.subject)
        statement = (
            insert(ToolSetSnapshotRow)
            .values(
                id=str(snapshot.tool_set_id),
                schema_version=snapshot.schema_version,
                tool_set_version=str(snapshot.tool_set_version),
                experiment_id=str(snapshot.experiment_id),
                subject_kind=subject_kind,
                subject_id=subject_id,
                subject_role=subject_role,
                phase=snapshot.phase.value,
                hardware_capabilities_json=list(snapshot.hardware_capabilities),
                enabled_providers_json=list(snapshot.enabled_providers),
                enabled_feature_flags_json=list(snapshot.enabled_feature_flags),
                tools_json=[entry.model_dump(mode="json") for entry in snapshot.tools],
                policy_decision_ids_json=list(snapshot.policy_decision_ids),
                created_at=snapshot.created_at,
            )
            .on_conflict_do_nothing(index_elements=[ToolSetSnapshotRow.id])
            .returning(ToolSetSnapshotRow.id)
        )
        if self._session.execute(statement).scalar_one_or_none() is not None:
            return
        existing = self._session.get(ToolSetSnapshotRow, str(snapshot.tool_set_id))
        if existing is None or _toolset_snapshot(existing) != snapshot:
            raise AuthorizationBindingError(AUTHORIZATION_BINDING_MISMATCH)

    def get(self, tool_set_id: ToolSetId) -> ToolSetSnapshot | None:
        row = self._session.get(ToolSetSnapshotRow, str(tool_set_id))
        return _toolset_snapshot(row) if row is not None else None


class SqlAlchemyPlanAuthorizationRepository:
    """Load the immutable Plan binding and complete persisted execution budget."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_for_execution(
        self,
        *,
        experiment_id: ExperimentId,
        plan_id: PlanId,
        expected_plan_hash: PlanHash,
    ) -> PlanAuthorizationMaterial:
        row = self._session.scalar(
            select(PlanRow)
            .where(
                PlanRow.experiment_id == str(experiment_id),
                PlanRow.id == str(plan_id),
                PlanRow.plan_hash == str(expected_plan_hash),
            )
            .with_for_update()
        )
        if row is None or row.status != PlanStatus.APPROVED.value:
            raise PlanAuthorizationError(PLAN_AUTHORIZATION_INVALID)
        try:
            body = _PlanBodyProjection.model_validate(row.body_json)
            return PlanAuthorizationMaterial(
                experiment_id=ExperimentId(root=row.experiment_id),
                plan_id=PlanId(root=row.id),
                plan_hash=PlanHash(root=row.plan_hash),
                status=PlanStatus(row.status),
                risk_level=RiskLevel(row.risk_level),
                execution_schema_version=body.execution_specification.schema_version,
                budget=body.execution_specification.budget,
            )
        except (ValidationError, ValueError) as error:
            raise PlanAuthorizationError(PLAN_AUTHORIZATION_INVALID) from error


def _job_authorization(row: JobAuthorizationRow) -> JobAuthorizationRecord:
    try:
        return JobAuthorizationRecord(
            schema_version=row.schema_version,
            job_id=JobId(root=row.job_id),
            experiment_id=ExperimentId(root=row.experiment_id),
            subject=_subject_from_columns(
                kind=row.subject_kind,
                subject_id=row.subject_id,
                role=row.subject_role,
            ),
            action=row.action,
            risk_level=RiskLevel(row.risk_level),
            plan_id=PlanId(root=row.plan_id),
            plan_hash=PlanHash(root=row.plan_hash),
            approval_id=row.approval_id,
            tool_schema_version=row.tool_schema_version,
            tool_set_id=ToolSetId(root=row.tool_set_id),
            tool_set_version=row.tool_set_version,
            policy_decision_id=row.policy_decision_id,
            request_hash=row.request_hash,
            idempotency_key=row.idempotency_key,
            authorized_at=row.authorized_at,
        )
    except (ValidationError, ValueError) as error:
        raise AuthorizationBindingError(AUTHORIZATION_BINDING_MISMATCH) from error


class SqlAlchemyJobAuthorizationRepository:
    """Persist one immutable authorization evidence record for each queued Job."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, authorization: JobAuthorizationRecord) -> None:
        existing = self._session.get(JobAuthorizationRow, str(authorization.job_id))
        if existing is not None:
            if _job_authorization(existing) != authorization:
                raise AuthorizationBindingError(AUTHORIZATION_BINDING_MISMATCH)
            return
        subject_kind, subject_id, subject_role = _subject_columns(authorization.subject)
        statement = (
            insert(JobAuthorizationRow)
            .values(
                job_id=str(authorization.job_id),
                schema_version=authorization.schema_version,
                experiment_id=str(authorization.experiment_id),
                subject_kind=subject_kind,
                subject_id=subject_id,
                subject_role=subject_role,
                action=str(authorization.action),
                risk_level=authorization.risk_level.value,
                plan_id=str(authorization.plan_id),
                plan_hash=str(authorization.plan_hash),
                approval_id=(
                    str(authorization.approval_id)
                    if authorization.approval_id is not None
                    else None
                ),
                tool_schema_version=authorization.tool_schema_version,
                tool_set_id=str(authorization.tool_set_id),
                tool_set_version=str(authorization.tool_set_version),
                policy_decision_id=authorization.policy_decision_id,
                request_hash=str(authorization.request_hash),
                idempotency_key=str(authorization.idempotency_key),
                authorized_at=authorization.authorized_at,
            )
            .on_conflict_do_nothing(index_elements=[JobAuthorizationRow.job_id])
            .returning(JobAuthorizationRow.job_id)
        )
        if self._session.execute(statement).scalar_one_or_none() is not None:
            return
        existing = self._session.get(JobAuthorizationRow, str(authorization.job_id))
        if existing is None or _job_authorization(existing) != authorization:
            raise AuthorizationBindingError(AUTHORIZATION_BINDING_MISMATCH)

    def get(self, job_id: JobId) -> JobAuthorizationRecord | None:
        row = self._session.get(JobAuthorizationRow, str(job_id))
        return _job_authorization(row) if row is not None else None


def replay_authorization_matches(
    existing: JobAuthorizationRecord,
    requested: JobAuthorizationDraft,
) -> bool:
    """Compare immutable request identity while preserving the original authorizer evidence."""
    return (
        existing.experiment_id == requested.experiment_id
        and existing.action == requested.action
        and existing.risk_level is requested.risk_level
        and existing.plan_id == requested.plan_id
        and existing.plan_hash == requested.plan_hash
        and existing.tool_schema_version == requested.tool_schema_version
        and existing.request_hash == requested.request_hash
        and existing.idempotency_key == requested.idempotency_key
    )


def _advisory_lock_key(digest: str) -> int:
    unsigned = int(digest.removeprefix("sha256:")[:16], 16)
    return unsigned if unsigned < 2**63 else unsigned - 2**64


class SqlAlchemyJobIdempotencyGate:
    """Use a transaction advisory lock before resource reservation and Job enqueue."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def claim(self, authorization: JobAuthorizationDraft) -> JobIdempotencyClaim:
        try:
            self._session.execute(
                select(
                    func.pg_advisory_xact_lock(
                        _advisory_lock_key(str(authorization.idempotency_key))
                    )
                )
            )
            row = self._session.get(IdempotencyRow, str(authorization.idempotency_key))
            if row is None:
                return JobIdempotencyClaim(
                    idempotency_key=authorization.idempotency_key,
                )
            job = self._session.get(JobRow, row.job_id)
            existing = SqlAlchemyJobAuthorizationRepository(self._session).get(
                JobId(root=row.job_id)
            )
        except (SQLAlchemyError, AuthorizationBindingError, ValidationError) as error:
            raise IdempotencyAuthorizationError(IDEMPOTENCY_BINDING_MISMATCH) from error
        if (
            row.experiment_id != str(authorization.experiment_id)
            or row.request_hash != str(authorization.request_hash)
            or row.action != str(authorization.action)
            or job is None
            or existing is None
            or not replay_authorization_matches(existing, authorization)
        ):
            raise IdempotencyAuthorizationError(IDEMPOTENCY_BINDING_MISMATCH)
        return JobIdempotencyClaim(
            idempotency_key=authorization.idempotency_key,
            existing_job_id=existing.job_id,
        )
