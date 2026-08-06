from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr

from autopilot.api.app import ApiDependencies, create_app
from autopilot.api.repositories import InMemoryExperimentStore
from autopilot.domain.enums import UserRole
from autopilot.domain.identifiers import UserId
from autopilot.domain.identities import BearerTokenBinding, HumanSubject
from autopilot.gateway.authentication import (
    BearerTokenAuthenticator,
    hash_bearer_token,
)
from autopilot.graph.reconciliation import NoopReconciler
from autopilot.graph.workflow import GraphDependencies, build_runtime
from tests.unit.graph.fakes import FakeGraphOperations, FakeModelProvider

TOKEN = "A" * 43


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
    authenticator = BearerTokenAuthenticator(
        (
            BearerTokenBinding(
                token_hash=hash_bearer_token(SecretStr(TOKEN)),
                subject=subject,
            ),
        )
    )
    return ApiDependencies(
        authenticator=authenticator,
        experiments=experiments,
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
