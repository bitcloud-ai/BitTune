from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from runner.api import create_runner_app
from runner.errors import RunnerConfigurationError, RunnerValidationError
from runner.models import OperationResponse, RunnerRequest, RunnerResponse, SecretRef
from runner.secrets import SystemdCredentialResolver
from runner.uds import UnixSocketEndpoint
from tests.unit.runner.conftest import DEPLOYMENT_ID, DIGEST, PLAN_ID, start_deployment_data


def test_systemd_secret_resolution_uses_logical_name_and_masks_value(tmp_path: Path) -> None:
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    secret_value = b"never-log-this-token"
    (credentials / "huggingface-token").write_bytes(secret_value)
    resolver = SystemdCredentialResolver(credentials)
    assert resolver.resolve(SecretRef(root="huggingface-token")) == secret_value

    with pytest.raises(RunnerValidationError) as caught:
        resolver.resolve(SecretRef(root="missing-token"))
    assert secret_value.decode() not in str(caught.value)


def test_uds_endpoint_is_fixed_under_absolute_runtime_root(tmp_path: Path) -> None:
    endpoint = UnixSocketEndpoint(runtime_root=tmp_path / "runtime")
    assert endpoint.path == (tmp_path / "runtime" / "runner.sock").resolve()
    with pytest.raises(RunnerConfigurationError):
        UnixSocketEndpoint(runtime_root=tmp_path / "runtime", socket_name="other.sock")


def test_uds_stale_path_must_be_a_socket(tmp_path: Path) -> None:
    endpoint = UnixSocketEndpoint(runtime_root=tmp_path / "runtime")
    endpoint.path.write_text("not-a-socket", encoding="utf-8")
    with pytest.raises(RunnerConfigurationError):
        endpoint.remove_stale_socket()


class _DispatchStub:
    def dispatch(self, request: RunnerRequest) -> RunnerResponse:
        return RunnerResponse(
            request_id=request.request_id,
            accepted=True,
            result=OperationResponse(
                resource_id=request.plan_id,
                state="accepted",
            ),
        )


def test_runner_api_uses_typed_rest_routes() -> None:
    app = create_runner_app(_DispatchStub())
    client = TestClient(app)
    response = client.post("/runner/v1/deployments/start", json=start_deployment_data())
    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert "/runner/v1/deployments/{deployment_id}" in app.openapi()["paths"]


def test_runner_api_rejects_untyped_or_unknown_fields() -> None:
    app = create_runner_app(_DispatchStub())
    response = TestClient(app).post(
        "/runner/v1/deployments/start",
        json={**start_deployment_data(), "command": "echo unsafe"},
    )
    assert response.status_code == 422


def test_runner_api_maps_invalid_get_metadata_to_structured_4xx() -> None:
    app = create_runner_app(_DispatchStub())
    response = TestClient(app).get(
        f"/runner/v1/deployments/{DEPLOYMENT_ID}",
        params={
            "request_id": "bad",
            "idempotency_key": DIGEST,
            "actor": "autopilot-worker",
            "plan_id": PLAN_ID,
            "plan_hash": DIGEST,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "RUNNER_REQUEST_INVALID"
