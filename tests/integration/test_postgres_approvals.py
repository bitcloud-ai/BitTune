from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from autopilot.domain.enums import ApprovalDecision, PlanStatus, UserRole
from autopilot.domain.identifiers import (
    ApprovalId,
    ExperimentId,
    PlanHash,
    PlanId,
    ToolName,
    UserId,
)
from autopilot.domain.identities import HumanSubject
from autopilot.gateway.approval_ports import CreateApprovalRequest, DecideApprovalRequest
from autopilot.infrastructure.database.base import APP_SCHEMA
from autopilot.infrastructure.database.errors import ApprovalActorConflictError
from autopilot.infrastructure.database.models import ApprovalRow, ExperimentRow, PlanRow
from autopilot.infrastructure.database.repositories import SqlAlchemyApprovalRepository
from autopilot.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)

_DATABASE_URL = os.environ.get("AUTOPILOT_TEST_POSTGRES_URL")
_PROJECT_ROOT = Path(__file__).parents[2]


def _alembic_config(connection: object) -> Config:
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


@pytest.fixture(scope="module")
def postgres_engine() -> Engine:
    if _DATABASE_URL is None:
        pytest.skip("AUTOPILOT_TEST_POSTGRES_URL is not configured")
    engine = create_postgres_engine(_DATABASE_URL)
    version_table_existed = False
    with engine.connect() as connection:
        inspector = inspect(connection)
        if inspector.has_schema(APP_SCHEMA):
            pytest.fail("PostgreSQL integration tests require a database without the app schema")
        version_rows = ()
        version_table_existed = inspector.has_table("alembic_version")
        if version_table_existed:
            version_rows = tuple(
                connection.execute(text("SELECT version_num FROM alembic_version"))
            )
        if version_rows:
            pytest.fail("PostgreSQL integration tests require an unversioned disposable database")
    yield engine
    with engine.begin() as connection:
        if not version_table_existed and inspect(connection).has_table("alembic_version"):
            connection.execute(text("DROP TABLE alembic_version"))
    engine.dispose()


@pytest.fixture(autouse=True)
def migrated_database(postgres_engine: Engine):
    with postgres_engine.connect() as connection:
        command.upgrade(_alembic_config(connection), "head")
    try:
        yield
    finally:
        with postgres_engine.connect() as connection:
            command.downgrade(_alembic_config(connection), "base")


@pytest.fixture
def session_factory(postgres_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(postgres_engine)


def _plan_hash(value: str) -> PlanHash:
    return PlanHash(root=f"sha256:{hashlib.sha256(value.encode()).hexdigest()}")


def _seed_l2_plan(session: Session) -> tuple[ExperimentId, PlanId, PlanHash]:
    now = datetime.now(UTC)
    experiment_id = ExperimentId.new()
    plan_id = PlanId.new()
    plan_hash = _plan_hash(str(plan_id))
    session.add(
        ExperimentRow(
            id=str(experiment_id),
            status="active",
            phase="approval",
            created_by=str(UserId.new()),
            requirements_json={},
            created_at=now,
            updated_at=now,
        )
    )
    session.add(
        PlanRow(
            id=str(plan_id),
            experiment_id=str(experiment_id),
            kind="deployment",
            schema_version="deployment-plan/v1",
            body_json={},
            plan_hash=str(plan_hash),
            risk_level="L2",
            status=PlanStatus.DRAFT.value,
            approved_by=None,
            created_at=now,
        )
    )
    session.flush()
    return experiment_id, plan_id, plan_hash


def _create_request(
    *,
    experiment_id: ExperimentId,
    plan_id: PlanId,
    plan_hash: PlanHash,
    requester: HumanSubject,
    expires_in: timedelta = timedelta(minutes=15),
) -> CreateApprovalRequest:
    return CreateApprovalRequest(
        approval_id=ApprovalId.new(),
        experiment_id=experiment_id,
        plan_id=plan_id,
        expected_plan_hash=plan_hash,
        action=ToolName(root="start_deployment"),
        requester=requester,
        expires_in=expires_in,
    )


def test_approval_decision_updates_plan_in_same_transaction(
    session_factory: sessionmaker[Session],
) -> None:
    requester = HumanSubject(user_id=UserId.new(), role=UserRole.OPERATOR)
    actor = HumanSubject(user_id=UserId.new(), role=UserRole.ADMIN)
    with session_factory.begin() as session:
        experiment_id, plan_id, plan_hash = _seed_l2_plan(session)
        approval = SqlAlchemyApprovalRepository(session).create(
            _create_request(
                experiment_id=experiment_id,
                plan_id=plan_id,
                plan_hash=plan_hash,
                requester=requester,
            )
        )

    with session_factory.begin() as session:
        decided = SqlAlchemyApprovalRepository(session).decide(
            DecideApprovalRequest(
                approval_id=approval.approval_id,
                experiment_id=experiment_id,
                expected_plan_id=plan_id,
                expected_plan_hash=plan_hash,
                expected_action=approval.action,
                actor=actor,
                decision=ApprovalDecision.APPROVED,
                comment="approved after human review",
            )
        )
        assert decided.decision is ApprovalDecision.APPROVED
        assert decided.decided_by == actor

    with session_factory() as session:
        row = session.get(ApprovalRow, str(approval.approval_id))
        plan = session.get(PlanRow, str(plan_id))
        assert row is not None
        assert row.decision == ApprovalDecision.APPROVED.value
        assert plan is not None
        assert plan.status == PlanStatus.APPROVED.value
        assert plan.approved_by == str(actor.user_id)


def test_execution_revalidation_uses_persisted_approval_binding(
    session_factory: sessionmaker[Session],
) -> None:
    requester = HumanSubject(user_id=UserId.new(), role=UserRole.OPERATOR)
    actor = HumanSubject(user_id=UserId.new(), role=UserRole.ADMIN)
    with session_factory.begin() as session:
        experiment_id, plan_id, plan_hash = _seed_l2_plan(session)
        repository = SqlAlchemyApprovalRepository(session)
        approval = repository.create(
            _create_request(
                experiment_id=experiment_id,
                plan_id=plan_id,
                plan_hash=plan_hash,
                requester=requester,
            )
        )
        repository.decide(
            DecideApprovalRequest(
                approval_id=approval.approval_id,
                experiment_id=experiment_id,
                expected_plan_id=plan_id,
                expected_plan_hash=plan_hash,
                expected_action=approval.action,
                actor=actor,
                decision=ApprovalDecision.APPROVED,
            )
        )

    with session_factory.begin() as session:
        record = SqlAlchemyApprovalRepository(session).require_valid_for_execution(
            approval_id=approval.approval_id,
            experiment_id=experiment_id,
            plan_id=plan_id,
            expected_plan_hash=plan_hash,
            action=approval.action,
        )

    assert record.decision is ApprovalDecision.APPROVED
    assert record.requester == requester
    assert record.decided_by == actor


def test_repository_rejects_self_approval_and_persists_expiry(
    session_factory: sessionmaker[Session],
) -> None:
    requester = HumanSubject(user_id=UserId.new(), role=UserRole.OPERATOR)
    with session_factory.begin() as session:
        experiment_id, plan_id, plan_hash = _seed_l2_plan(session)
        approval = SqlAlchemyApprovalRepository(session).create(
            _create_request(
                experiment_id=experiment_id,
                plan_id=plan_id,
                plan_hash=plan_hash,
                requester=requester,
                expires_in=timedelta(microseconds=1),
            )
        )

    with session_factory.begin() as session, pytest.raises(ApprovalActorConflictError):
        SqlAlchemyApprovalRepository(session).decide(
            DecideApprovalRequest(
                approval_id=approval.approval_id,
                experiment_id=experiment_id,
                expected_plan_id=plan_id,
                expected_plan_hash=plan_hash,
                expected_action=approval.action,
                actor=HumanSubject(user_id=requester.user_id, role=UserRole.ADMIN),
                decision=ApprovalDecision.APPROVED,
            )
        )

    with session_factory.begin() as session:
        expired = SqlAlchemyApprovalRepository(session).decide(
            DecideApprovalRequest(
                approval_id=approval.approval_id,
                experiment_id=experiment_id,
                expected_plan_id=plan_id,
                expected_plan_hash=plan_hash,
                expected_action=approval.action,
                actor=HumanSubject(user_id=UserId.new(), role=UserRole.ADMIN),
                decision=ApprovalDecision.APPROVED,
            )
        )
        assert expired.decision is ApprovalDecision.EXPIRED

    with session_factory() as session:
        row = session.get(ApprovalRow, str(approval.approval_id))
        plan = session.get(PlanRow, str(plan_id))
        assert row is not None
        assert row.decision == ApprovalDecision.EXPIRED.value
        assert row.decided_by_kind is None
        assert row.decided_by_id is None
        assert row.decided_by_role is None
        assert plan is not None
        assert plan.status == PlanStatus.DRAFT.value
        assert plan.approved_by is None


def test_approval_binding_is_unique_and_terminal_rows_are_immutable(
    session_factory: sessionmaker[Session],
    postgres_engine: Engine,
) -> None:
    requester = HumanSubject(user_id=UserId.new(), role=UserRole.OPERATOR)
    actor = HumanSubject(user_id=UserId.new(), role=UserRole.ADMIN)
    with session_factory.begin() as session:
        experiment_id, plan_id, plan_hash = _seed_l2_plan(session)
        repository = SqlAlchemyApprovalRepository(session)
        approval = repository.create(
            _create_request(
                experiment_id=experiment_id,
                plan_id=plan_id,
                plan_hash=plan_hash,
                requester=requester,
            )
        )
        replay = repository.create(
            _create_request(
                experiment_id=experiment_id,
                plan_id=plan_id,
                plan_hash=plan_hash,
                requester=requester,
            )
        )
        assert replay.approval_id == approval.approval_id

    with session_factory.begin() as session:
        SqlAlchemyApprovalRepository(session).decide(
            DecideApprovalRequest(
                approval_id=approval.approval_id,
                experiment_id=experiment_id,
                expected_plan_id=plan_id,
                expected_plan_hash=plan_hash,
                expected_action=approval.action,
                actor=actor,
                decision=ApprovalDecision.REJECTED,
            )
        )

    statements = (
        "UPDATE app.approvals SET comment = 'changed'",
        "DELETE FROM app.approvals",
        "TRUNCATE app.approvals",
    )
    for statement in statements:
        with postgres_engine.connect() as connection:
            transaction = connection.begin()
            with pytest.raises(DBAPIError):
                connection.execute(text(statement))
            transaction.rollback()

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ApprovalRow)) == 1


def test_database_constraint_rejects_direct_self_approval(
    session_factory: sessionmaker[Session],
    postgres_engine: Engine,
) -> None:
    requester = HumanSubject(user_id=UserId.new(), role=UserRole.OPERATOR)
    with session_factory.begin() as session:
        experiment_id, plan_id, plan_hash = _seed_l2_plan(session)
        approval = SqlAlchemyApprovalRepository(session).create(
            _create_request(
                experiment_id=experiment_id,
                plan_id=plan_id,
                plan_hash=plan_hash,
                requester=requester,
            )
        )

    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(DBAPIError):
            connection.execute(
                text(
                    "UPDATE app.approvals SET decision = 'approved', "
                    "decided_by_kind = 'human', decided_by_id = :requester_id, "
                    "decided_by_role = 'admin', decided_at = clock_timestamp() "
                    "WHERE id = :approval_id"
                ),
                {
                    "requester_id": str(requester.user_id),
                    "approval_id": str(approval.approval_id),
                },
            )
        transaction.rollback()


@pytest.mark.parametrize(
    ("decider_kind", "decider_role"),
    [("service", "admin"), ("human", "operator")],
)
def test_database_constraint_rejects_non_human_or_non_admin_decider(
    session_factory: sessionmaker[Session],
    postgres_engine: Engine,
    decider_kind: str,
    decider_role: str,
) -> None:
    requester = HumanSubject(user_id=UserId.new(), role=UserRole.OPERATOR)
    with session_factory.begin() as session:
        experiment_id, plan_id, plan_hash = _seed_l2_plan(session)
        approval = SqlAlchemyApprovalRepository(session).create(
            _create_request(
                experiment_id=experiment_id,
                plan_id=plan_id,
                plan_hash=plan_hash,
                requester=requester,
            )
        )

    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(DBAPIError):
            connection.execute(
                text(
                    "UPDATE app.approvals SET decision = 'approved', "
                    "decided_by_kind = :decider_kind, decided_by_id = :decider_id, "
                    "decided_by_role = :decider_role, decided_at = clock_timestamp() "
                    "WHERE id = :approval_id"
                ),
                {
                    "approval_id": str(approval.approval_id),
                    "decider_id": str(UserId.new()),
                    "decider_kind": decider_kind,
                    "decider_role": decider_role,
                },
            )
        transaction.rollback()
