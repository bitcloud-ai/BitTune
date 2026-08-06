from __future__ import annotations

from types import TracebackType
from typing import ClassVar

import click
import httpx
import pytest
from click.testing import CliRunner

import autopilot.cli as cli_module
from autopilot.cli import ApiClient, cli

TOKEN = "A" * 43


class FakeCliClient:
    instances: ClassVar[list[FakeCliClient]] = []

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float,
    ) -> None:
        self.base_url = base_url
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.requests: list[tuple[str, str, dict[str, object] | None]] = []
        self.streamed: list[str] = []
        self.instances.append(self)

    def __enter__(self) -> FakeCliClient:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        return None

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
    ) -> object:
        self.requests.append((method, path, payload))
        return {"method": method, "path": path}

    def stream_events(self, experiment_id: str) -> None:
        self.streamed.append(experiment_id)
        click.echo("event: stream.end")


def _patch_fake_client(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeCliClient.instances.clear()
    monkeypatch.setattr(cli_module, "ApiClient", FakeCliClient)


def test_cli_commands_use_server_api_and_never_print_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOPILOT_API_TOKEN", TOKEN)
    _patch_fake_client(monkeypatch)
    runner = CliRunner()

    created = runner.invoke(cli, ["create", "tune a 7B model"])
    status = runner.invoke(cli, ["status", "exp_123"])
    resumed = runner.invoke(cli, ["resume", "exp_123", "--decision", "APPROVED"])
    events = runner.invoke(cli, ["events", "exp_123"])
    cancelled = runner.invoke(cli, ["cancel", "exp_123"])

    assert created.exit_code == 0
    assert status.exit_code == 0
    assert resumed.exit_code == 0
    assert events.exit_code == 0
    assert cancelled.exit_code == 0
    output = "\n".join(result.output for result in (created, status, resumed, events, cancelled))
    assert TOKEN not in output

    clients = FakeCliClient.instances
    assert len(clients) == 5
    assert clients[0].base_url == "http://127.0.0.1:8000"
    assert clients[0].token == TOKEN
    assert clients[0].requests == [
        (
            "POST",
            "/api/v1/experiments",
            {
                "schema_version": "create-experiment-request/v1",
                "message": "tune a 7B model",
            },
        )
    ]
    assert clients[1].requests == [("GET", "/api/v1/experiments/exp_123", None)]
    assert clients[2].requests == [
        (
            "POST",
            "/api/v1/experiments/exp_123/resume",
            {
                "schema_version": "experiment-resume-request/v1",
                "decision": "approved",
                "comment": None,
            },
        )
    ]
    assert clients[3].streamed == ["exp_123"]
    assert clients[4].requests == [("POST", "/api/v1/experiments/exp_123/cancel", None)]


def test_cli_requires_a_token_when_environment_value_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOPILOT_API_TOKEN", "")
    result = CliRunner().invoke(cli, ["status", "exp_123"])

    assert result.exit_code != 0
    assert "AUTOPILOT_API_TOKEN is empty" in result.output


def test_api_client_uses_bearer_token_and_decodes_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.method == "POST"
        assert request.url.path == "/api/v1/experiments"
        return httpx.Response(201, json={"experiment_id": "exp_123"}, request=request)

    transport = httpx.MockTransport(handler)
    with ApiClient(base_url="http://control-plane", token=TOKEN, transport=transport) as client:
        assert client.request_json("POST", "/api/v1/experiments", payload={"message": "run"}) == {
            "experiment_id": "exp_123"
        }


def test_api_client_maps_http_and_invalid_json_errors() -> None:
    def error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"code": "GRAPH_NOT_WAITING", "message": "not waiting"},
            request=request,
        )

    with (
        ApiClient(
            base_url="http://control-plane",
            token=TOKEN,
            transport=httpx.MockTransport(error_handler),
        ) as client,
        pytest.raises(click.ClickException, match="HTTP 409"),
    ):
        client.request_json("GET", "/api/v1/experiments/exp_123")

    def invalid_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json", request=request)

    with (
        ApiClient(
            base_url="http://control-plane",
            token=TOKEN,
            transport=httpx.MockTransport(invalid_handler),
        ) as client,
        pytest.raises(click.ClickException, match="invalid JSON"),
    ):
        client.request_json("GET", "/api/v1/experiments/exp_123")


def test_api_client_streams_sse_lines(capsys: pytest.CaptureFixture[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"event: graph.state\ndata: {}\n\n",
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    with ApiClient(
        base_url="http://control-plane",
        token=TOKEN,
        transport=httpx.MockTransport(handler),
    ) as client:
        client.stream_events("exp_123")

    assert "event: graph.state" in capsys.readouterr().out
