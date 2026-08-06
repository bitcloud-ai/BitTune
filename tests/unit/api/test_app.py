from dataclasses import replace

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr

from autopilot.api.app import ApiDependencies, create_app
from autopilot.api.repositories import InMemoryExperimentStore
from autopilot.domain.approvals import ApprovalRecord
from autopilot.domain.base import utc_now
from autopilot.domain.enums import ApprovalDecision, UserRole
from autopilot.domain.identifiers import ApprovalId, PlanHash, PlanId, ToolName, UserId
from autopilot.domain.identities import BearerTokenBinding, HumanSubject
from autopilot.gateway.approval_ports import CreateApprovalRequest, DecideApprovalRequest
from autopilot.gateway.authentication import (
    BearerTokenAuthenticator,
    hash_bearer_token,
)
from autopilot.gateway.models import GatewayEnvironment
from autopilot.graph.agent import AgentMessageView, AgentRunResult, AgentStreamEvent
from autopilot.graph.reconciliation import NoopReconciler
from autopilot.graph.workflow import GraphDependencies, build_runtime
from tests.unit.graph.fakes import FakeGraphOperations, FakeModelProvider

TOKEN = "A" * 43
ADMIN_TOKEN = "B" * 43
PLAN_ID = PlanId(root="plan_" + "1" * 32)
PLAN_HASH = PlanHash(root="sha256:" + "2" * 64)
PLAN_ACTION = ToolName(root="start_deployment")
APPROVAL_NOT_FOUND = "approval does not exist"
APPROVAL_SELF_DECISION = "requester cannot decide approval"
APPROVAL_BINDING_MISMATCH = "approval binding mismatch"


class InMemoryApprovalStore:
    def __init__(self) -> None:
        self.records: dict[ApprovalId, ApprovalRecord] = {}

    def create(self, request: CreateApprovalRequest) -> ApprovalRecord:
        for record in self.records.values():
            if (
                record.experiment_id == request.experiment_id
                and record.plan_id == request.plan_id
                and record.plan_hash == request.expected_plan_hash
                and record.action == request.action
            ):
                return record
        now = utc_now()
        record = ApprovalRecord(
            approval_id=request.approval_id,
            experiment_id=request.experiment_id,
            plan_id=request.plan_id,
            plan_hash=request.expected_plan_hash,
            action=request.action,
            requester=request.requester,
            requested_at=now,
            expires_at=now + request.expires_in,
            comment=request.comment,
        )
        self.records[record.approval_id] = record
        return record

    def get(self, approval_id: str) -> ApprovalRecord | None:
        try:
            return self.records.get(ApprovalId(root=approval_id))
        except ValueError:
            return None

    def decide(self, request: DecideApprovalRequest) -> ApprovalRecord:
        record = self.records.get(request.approval_id)
        if record is None:
            raise ValueError(APPROVAL_NOT_FOUND)
        if record.requester.user_id == request.actor.user_id:
            raise ValueError(APPROVAL_SELF_DECISION)
        if (
            record.experiment_id != request.experiment_id
            or record.plan_id != request.expected_plan_id
            or record.plan_hash != request.expected_plan_hash
            or record.action != request.expected_action
        ):
            raise ValueError(APPROVAL_BINDING_MISMATCH)
        if record.decision is not ApprovalDecision.PENDING:
            return record
        updated = record.model_copy(
            update={
                "decision": request.decision,
                "decided_by": request.actor,
                "decided_at": utc_now(),
                "comment": request.comment,
            }
        )
        self.records[record.approval_id] = updated
        return updated


class FakeAgent:
    def __init__(self) -> None:
        self.messages: dict[str, tuple[AgentMessageView, ...]] = {}

    def send(
        self,
        *,
        experiment_id,
        message: str,
        environment: GatewayEnvironment,
    ) -> AgentRunResult:
        del environment
        current = self.messages.get(str(experiment_id), ())
        updated = (
            *current,
            AgentMessageView(role="user", content=message),
            AgentMessageView(
                role="assistant",
                content="I need approval before deployment."
                if "deploy" in message
                else "Environment is ready.",
            ),
        )
        self.messages[str(experiment_id)] = updated
        interrupted = "deploy" in message
        return AgentRunResult(
            messages=updated,
            tool_calls=(),
            interrupted=interrupted,
            interrupt_payload=(
                {
                    "action_requests": [
                        {
                            "name": str(PLAN_ACTION),
                            "args": {
                                "plan_id": str(PLAN_ID),
                                "expected_plan_hash": str(PLAN_HASH),
                            },
                        }
                    ]
                }
                if interrupted
                else None
            ),
            tool_set_id=None,
            tool_set_version=None,
        )

    def resume(
        self,
        *,
        experiment_id,
        approved: bool,
        environment: GatewayEnvironment,
        message: str | None = None,
    ) -> AgentRunResult:
        del environment, message
        current = self.messages[str(experiment_id)]
        updated = (
            *current,
            AgentMessageView(
                role="assistant",
                content="Deployment started." if approved else "Deployment rejected.",
            ),
        )
        self.messages[str(experiment_id)] = updated
        return AgentRunResult(
            messages=updated,
            tool_calls=(),
            interrupted=False,
            interrupt_payload=None,
            tool_set_id=None,
            tool_set_version=None,
        )

    def state(self, *, experiment_id) -> tuple[AgentMessageView, ...]:
        return self.messages.get(str(experiment_id), ())

    def stream_resume(
        self,
        *,
        experiment_id,
        approved: bool,
        environment: GatewayEnvironment,
        message: str | None = None,
    ):
        result = self.resume(
            experiment_id=experiment_id,
            approved=approved,
            environment=environment,
            message=message,
        )
        yield AgentStreamEvent(
            event_type="run.completed",
            payload={"interrupted": False},
            result=result,
        )


class FailingStreamAgent(FakeAgent):
    def stream_resume(self, **kwargs):
        del kwargs
        yield AgentStreamEvent(
            event_type="run.error",
            payload={"code": "MODEL_PROVIDER_UNAVAILABLE"},
        )


def _client() -> TestClient:
    subject = HumanSubject(user_id=UserId(root="user_" + "2" * 32), role=UserRole.OPERATOR)
    authenticator = BearerTokenAuthenticator(
        (
            BearerTokenBinding(
                token_hash=hash_bearer_token(SecretStr(TOKEN)),
                subject=subject,
            ),
        )
    )
    graph = build_runtime(
        GraphDependencies(FakeModelProvider(), FakeGraphOperations(), NoopReconciler()),
        checkpointer=InMemorySaver(),
    )
    return TestClient(
        create_app(
            ApiDependencies(
                authenticator=authenticator,
                experiments=InMemoryExperimentStore(),
                graph=graph,
            )
        )
    )


def _dependencies(saver: InMemorySaver, experiments: InMemoryExperimentStore) -> ApiDependencies:
    subject = HumanSubject(user_id=UserId(root="user_" + "2" * 32), role=UserRole.OPERATOR)
    admin = HumanSubject(user_id=UserId(root="user_" + "3" * 32), role=UserRole.ADMIN)
    authenticator = BearerTokenAuthenticator(
        (
            BearerTokenBinding(
                token_hash=hash_bearer_token(SecretStr(TOKEN)),
                subject=subject,
            ),
            BearerTokenBinding(
                token_hash=hash_bearer_token(SecretStr(ADMIN_TOKEN)),
                subject=admin,
            ),
        )
    )
    return ApiDependencies(
        authenticator=authenticator,
        experiments=experiments,
        approvals=InMemoryApprovalStore(),
        graph=build_runtime(
            GraphDependencies(FakeModelProvider(), FakeGraphOperations(), NoopReconciler()),
            checkpointer=saver,
        ),
    )


def test_api_requires_bearer_and_exposes_interrupt_and_sse() -> None:
    client = _client()
    unauthorized = client.post(
        "/api/v1/experiments",
        json={"schema_version": "create-experiment-request/v1", "message": "run"},
    )
    assert unauthorized.status_code == 401

    response = client.post(
        "/api/v1/experiments",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"schema_version": "create-experiment-request/v1", "message": "run"},
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["interrupted"] is True
    experiment_id = payload["experiment"]["experiment_id"]

    events = client.get(
        f"/api/v1/experiments/{experiment_id}/events",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert events.status_code == 200
    assert events.headers["content-type"].startswith("text/event-stream")
    assert "event: graph.state" in events.text


def test_openapi_contains_the_documented_m8_resources() -> None:
    paths = _client().get("/openapi.json").json()["paths"]
    expected = {
        "/api/v1/experiments",
        "/api/v1/experiments/{experiment_id}",
        "/api/v1/experiments/{experiment_id}/resume",
        "/api/v1/jobs/{job_id}",
        "/api/v1/plans/{plan_id}",
        "/api/v1/deployments/{deployment_id}",
        "/api/v1/artifacts/{artifact_id}/metadata",
    }
    assert expected.issubset(paths)


def test_api_resume_uses_the_same_checkpoint_after_runtime_restart() -> None:
    saver = InMemorySaver()
    experiments = InMemoryExperimentStore()
    first_client = TestClient(create_app(_dependencies(saver, experiments)))
    created = first_client.post(
        "/api/v1/experiments",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"schema_version": "create-experiment-request/v1", "message": "run"},
    )
    assert created.status_code == 201
    experiment_id = created.json()["experiment"]["experiment_id"]

    restarted_client = TestClient(create_app(_dependencies(saver, experiments)))
    resumed = restarted_client.post(
        f"/api/v1/experiments/{experiment_id}/resume",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"schema_version": "experiment-resume-request/v1", "decision": "approved"},
    )
    assert resumed.status_code == 200
    assert resumed.json()["interrupt"]["action"] == "approve_champion"


def test_agent_session_api_is_continuous_and_resumable() -> None:
    saver = InMemorySaver()
    experiments = InMemoryExperimentStore()
    agent = FakeAgent()
    dependencies = replace(_dependencies(saver, experiments), agent=agent)
    client = TestClient(create_app(dependencies))
    headers = {"Authorization": f"Bearer {TOKEN}"}

    created = client.post(
        "/api/v1/sessions",
        headers=headers,
        json={"schema_version": "create-session-request/v1", "message": "deploy now"},
    )
    assert created.status_code == 201
    session = created.json()
    assert session["interrupted"] is True
    experiment_id = session["experiment_id"]

    blocked = client.post(
        f"/api/v1/sessions/{experiment_id}/messages",
        headers=headers,
        json={"schema_version": "session-message-request/v1", "message": "status"},
    )
    assert blocked.status_code == 409

    self_approval = client.post(
        f"/api/v1/sessions/{experiment_id}/resume",
        headers=headers,
        json={"schema_version": "session-resume-request/v1", "decision": "approve"},
    )
    assert self_approval.status_code == 403

    resumed = client.post(
        f"/api/v1/sessions/{experiment_id}/resume",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        json={"schema_version": "session-resume-request/v1", "decision": "approve"},
    )
    assert resumed.status_code == 200
    assert resumed.json()["interrupted"] is False

    events = client.get(
        f"/api/v1/sessions/{experiment_id}/events",
        headers=headers,
    )
    assert events.status_code == 200
    assert events.headers["content-type"].startswith("text/event-stream")
    assert "event: agent.message" in events.text

    continued = client.post(
        f"/api/v1/sessions/{experiment_id}/messages",
        headers=headers,
        json={"schema_version": "session-message-request/v1", "message": "status"},
    )
    assert continued.status_code == 200
    assert len(continued.json()["messages"]) == 5


def test_agent_stream_resume_persists_independent_approval() -> None:
    saver = InMemorySaver()
    experiments = InMemoryExperimentStore()
    approvals = InMemoryApprovalStore()
    agent = FakeAgent()
    dependencies = replace(
        _dependencies(saver, experiments),
        agent=agent,
        approvals=approvals,
    )
    client = TestClient(create_app(dependencies))
    created = client.post(
        "/api/v1/sessions",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"schema_version": "create-session-request/v1", "message": "deploy now"},
    )
    assert created.status_code == 201
    experiment_id = created.json()["experiment_id"]
    assert len(approvals.records) == 1

    resumed = client.post(
        f"/api/v1/sessions/{experiment_id}/resume/stream",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        json={"schema_version": "session-resume-request/v1", "decision": "approve"},
    )

    assert resumed.status_code == 200
    assert "event: run.completed" in resumed.text
    approval = next(iter(approvals.records.values()))
    assert approval.decision is ApprovalDecision.APPROVED


def test_agent_stream_resume_failure_restores_pending_session_projection() -> None:
    saver = InMemorySaver()
    experiments = InMemoryExperimentStore()
    approvals = InMemoryApprovalStore()
    dependencies = replace(
        _dependencies(saver, experiments),
        agent=FailingStreamAgent(),
        approvals=approvals,
    )
    client = TestClient(create_app(dependencies))
    created = client.post(
        "/api/v1/sessions",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"schema_version": "create-session-request/v1", "message": "deploy now"},
    )
    experiment_id = created.json()["experiment_id"]

    resumed = client.post(
        f"/api/v1/sessions/{experiment_id}/resume/stream",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        json={"schema_version": "session-resume-request/v1", "decision": "approve"},
    )
    session = client.get(
        f"/api/v1/sessions/{experiment_id}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert "event: run.error" in resumed.text
    assert session.json()["status"] == "waiting_approval"
    assert session.json()["phase"] == "approval"
