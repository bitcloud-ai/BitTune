"""Standalone systemd entrypoint for the Host Runner REST service."""

from __future__ import annotations

import asyncio
import logging

import uvicorn
from pydantic import ValidationError

from runner.api import create_runner_app
from runner.config import RunnerSettings
from runner.docker_sdk import DockerSdkAdapter
from runner.errors import RunnerServiceError
from runner.health import UnavailableVllmHealthProbe, VllmHealthVerifier
from runner.leases import GpuLeaseManager
from runner.logs import SecretRedactor, configure_runner_logging
from runner.models import StorageRoot
from runner.reconciliation import RunnerWatchdog, UnavailableReconciliationSource
from runner.service import RunnerService
from runner.uds import UnixSocketEndpoint


async def _serve() -> None:
    settings = RunnerSettings()
    compiler = settings.compiler()
    redactor = SecretRedactor()
    configure_runner_logging(redactor)
    service = RunnerService(
        docker=DockerSdkAdapter.from_environment(redactor=redactor),
        compiler=compiler,
        leases=GpuLeaseManager(),
        health=VllmHealthVerifier(UnavailableVllmHealthProbe()),
    )
    endpoint = UnixSocketEndpoint(runtime_root=compiler.roots.root(StorageRoot.RUNTIME))
    endpoint.remove_stale_socket()
    app = create_runner_app(service, endpoint=endpoint)
    watchdog = RunnerWatchdog(
        service=service,
        source=UnavailableReconciliationSource(),
        interval_seconds=settings.maintenance_interval_seconds,
    )
    startup_report = watchdog.run_once()
    if not startup_report.source_available:
        logging.warning(
            "authoritative reconciliation source unavailable; preserving managed containers"
        )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            uds=str(endpoint.path),
            log_config=None,
            access_log=False,
            server_header=False,
            date_header=False,
        )
    )
    stop_event = asyncio.Event()

    async def serve() -> None:
        try:
            await server.serve()
        finally:
            stop_event.set()

    async def maintain() -> None:
        try:
            await watchdog.run(stop_event)
        finally:
            server.should_exit = True

    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(serve())
            tasks.create_task(maintain())
    finally:
        endpoint.remove_stale_socket()


def main() -> int:
    """Run the UDS server and return a process exit status."""

    try:
        asyncio.run(_serve())
    except (RunnerServiceError, ValidationError, OSError) as error:
        logging.error("autopilot-runner startup failed: %s", error)  # noqa: TRY400
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
