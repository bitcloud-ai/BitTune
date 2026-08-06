"""Click-based command line client for the Autopilot REST/SSE control plane."""

from __future__ import annotations

import getpass
import json
import os
from contextlib import suppress
from dataclasses import dataclass
from types import TracebackType
from typing import cast

import click
import httpx

DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class CliConfig:
    base_url: str
    timeout_seconds: float


class ApiClient:
    """Small typed transport client; workflow and authorization remain server-side."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout_seconds,
            transport=transport,
        )

    def __enter__(self) -> ApiClient:
        self._client.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._client.__exit__(exc_type, exc_value, traceback)

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
    ) -> object:
        try:
            response = self._client.request(method, path, json=payload)
        except httpx.HTTPError as error:
            raise click.ClickException(f"API request failed: {error}") from error
        if not response.is_success:
            detail = response.text
            with suppress(ValueError, TypeError):
                detail = json.dumps(response.json(), ensure_ascii=False)
            raise click.ClickException(f"API returned HTTP {response.status_code}: {detail}")
        try:
            return response.json()
        except (ValueError, TypeError) as error:
            raise click.ClickException("API returned invalid JSON") from error

    def stream_events(self, experiment_id: str) -> None:
        try:
            with self._client.stream(
                "GET", f"/api/v1/experiments/{experiment_id}/events"
            ) as response:
                if not response.is_success:
                    raise click.ClickException(
                        f"API returned HTTP {response.status_code}: {response.text}"
                    )
                for line in response.iter_lines():
                    click.echo(line)
        except httpx.HTTPError as error:
            raise click.ClickException(f"API stream failed: {error}") from error


def _token() -> str:
    token = os.environ.get("AUTOPILOT_API_TOKEN")
    if token is None:
        token = getpass.getpass("Autopilot API token: ")
    if not token:
        raise click.ClickException(
            "AUTOPILOT_API_TOKEN is empty; set it or provide an interactive token"
        )
    return token


def _print_json(value: object) -> None:
    click.echo(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


@click.group()
@click.option(
    "--base-url",
    envvar="AUTOPILOT_API_URL",
    default=DEFAULT_API_URL,
    show_default=True,
    help="Autopilot API base URL.",
)
@click.option(
    "--timeout",
    "timeout_seconds",
    type=click.FloatRange(min=1.0, max=600.0),
    default=DEFAULT_TIMEOUT_SECONDS,
    show_default=True,
    help="HTTP timeout in seconds.",
)
@click.pass_context
def cli(ctx: click.Context, base_url: str, timeout_seconds: float) -> None:
    """Interact with the auditable Autopilot control plane."""
    ctx.ensure_object(dict)
    ctx.obj["config"] = CliConfig(
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def _config(ctx: click.Context) -> CliConfig:
    return cast(CliConfig, ctx.ensure_object(dict)["config"])


@cli.command("create")
@click.argument("message", required=True)
@click.pass_context
def create_experiment(ctx: click.Context, message: str) -> None:
    """Create an Experiment from one natural-language requirement."""
    config = _config(ctx)
    with ApiClient(
        base_url=config.base_url, token=_token(), timeout_seconds=config.timeout_seconds
    ) as client:
        result = client.request_json(
            "POST",
            "/api/v1/experiments",
            payload={
                "schema_version": "create-experiment-request/v1",
                "message": message,
            },
        )
    _print_json(result)


@cli.command("status")
@click.argument("experiment_id", required=True)
@click.pass_context
def experiment_status(ctx: click.Context, experiment_id: str) -> None:
    """Show one Experiment projection and its structured Graph State."""
    config = _config(ctx)
    with ApiClient(
        base_url=config.base_url, token=_token(), timeout_seconds=config.timeout_seconds
    ) as client:
        result = client.request_json("GET", f"/api/v1/experiments/{experiment_id}")
    _print_json(result)


@cli.command("resume")
@click.argument("experiment_id", required=True)
@click.option(
    "--decision",
    type=click.Choice(("approved", "rejected"), case_sensitive=False),
    required=True,
    help="Decision for the current Graph approval interrupt.",
)
@click.option("--comment", default=None, help="Optional audit comment.")
@click.pass_context
def resume_experiment(
    ctx: click.Context,
    experiment_id: str,
    decision: str,
    comment: str | None,
) -> None:
    """Resume an Experiment at its current approval interrupt."""
    config = _config(ctx)
    with ApiClient(
        base_url=config.base_url, token=_token(), timeout_seconds=config.timeout_seconds
    ) as client:
        result = client.request_json(
            "POST",
            f"/api/v1/experiments/{experiment_id}/resume",
            payload={
                "schema_version": "experiment-resume-request/v1",
                "decision": decision.lower(),
                "comment": comment,
            },
        )
    _print_json(result)


@cli.command("events")
@click.argument("experiment_id", required=True)
@click.pass_context
def experiment_events(ctx: click.Context, experiment_id: str) -> None:
    """Stream structured SSE events for an Experiment."""
    config = _config(ctx)
    with ApiClient(
        base_url=config.base_url, token=_token(), timeout_seconds=config.timeout_seconds
    ) as client:
        client.stream_events(experiment_id)


@cli.command("cancel")
@click.argument("experiment_id", required=True)
@click.pass_context
def cancel_experiment(ctx: click.Context, experiment_id: str) -> None:
    """Request cancellation of an Experiment."""
    config = _config(ctx)
    with ApiClient(
        base_url=config.base_url, token=_token(), timeout_seconds=config.timeout_seconds
    ) as client:
        result = client.request_json("POST", f"/api/v1/experiments/{experiment_id}/cancel")
    _print_json(result)


def main() -> None:
    """Installed console-script entry point."""
    cli(prog_name="autopilot")


__all__ = ["ApiClient", "CliConfig", "cli", "main"]
