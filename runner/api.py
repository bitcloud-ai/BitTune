"""FastAPI REST boundary for the privileged Host Runner.

Uvicorn binds this application to the configured Unix Domain Socket. The
application accepts only the documented typed actions; all execution remains
inside :class:`runner.service.RunnerService`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal, Protocol

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from pydantic import Field, ValidationError

from runner.models import (
    CancelBenchmarkRequest,
    DeploymentRefPayload,
    DeploymentStatusRequest,
    InspectEnvironmentRequest,
    JobArtifactsRequest,
    JobRefPayload,
    JobStatusRequest,
    RunnerError,
    RunnerModel,
    RunnerRequest,
    RunnerRequestId,
    RunnerResponse,
    Sha256Digest,
    StartBenchmarkRequest,
    StartDeploymentRequest,
    StopDeploymentRequest,
)
from runner.uds import UnixSocketEndpoint

INVALID_QUERY_MESSAGE = "query request metadata is invalid"
INVALID_DEPLOYMENT_ID_MESSAGE = "deployment identifier is invalid"
INVALID_JOB_ID_MESSAGE = "Job identifier is invalid"


class RunnerDispatcher(Protocol):
    """The only application service surface exposed to the HTTP layer."""

    def dispatch(self, request: RunnerRequest) -> RunnerResponse: ...


class RunnerQueryEnvelope(RunnerModel):
    """The immutable request metadata carried by documented GET actions."""

    request_id: RunnerRequestId
    idempotency_key: Sha256Digest
    actor: Literal["autopilot-api", "autopilot-worker"]
    plan_id: str = Field(min_length=3, max_length=128, pattern=r"^plan_[0-9a-f]{32}$")
    plan_hash: Sha256Digest


def _query_envelope(
    request_id: str = Query(...),
    idempotency_key: str = Query(...),
    actor: Literal["autopilot-api", "autopilot-worker"] = Query(...),
    plan_id: str = Query(...),
    plan_hash: str = Query(...),
) -> RunnerQueryEnvelope:
    """Validate GET metadata with the same Pydantic contract as POST actions."""

    try:
        return RunnerQueryEnvelope(
            request_id=request_id,
            idempotency_key=idempotency_key,
            actor=actor,
            plan_id=plan_id,
            plan_hash=plan_hash,
        )
    except ValidationError as error:
        raise _invalid_request(INVALID_QUERY_MESSAGE) from error


def _path_conflict() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=RunnerError(
            code="PATH_RESOURCE_MISMATCH",
            message="path resource does not match the typed request payload",
        ).model_dump(mode="json"),
    )


def _invalid_request(message: str) -> HTTPException:
    """Map boundary-model failures to a stable structured client error."""

    return HTTPException(
        status_code=422,
        detail=RunnerError(
            code="RUNNER_REQUEST_INVALID",
            message=message,
        ).model_dump(mode="json"),
    )


def _dispatch(service: RunnerDispatcher, request: RunnerRequest) -> RunnerResponse:
    """Dispatch only a request already validated by FastAPI/Pydantic."""

    return service.dispatch(request)


def create_runner_app(
    service: RunnerDispatcher,
    *,
    endpoint: UnixSocketEndpoint | None = None,
) -> FastAPI:
    """Build the Runner API without creating a TCP listener."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if endpoint is not None and endpoint.path.exists():
            endpoint.path.chmod(0o660)
        yield

    app = FastAPI(
        title="BitTune Autopilot Host Runner",
        version="runner-api/v1",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    router = APIRouter(prefix="/runner/v1")

    @router.post("/environment/inspect", response_model=RunnerResponse)
    def inspect_environment(request: InspectEnvironmentRequest) -> RunnerResponse:
        return _dispatch(service, request)

    @router.post("/deployments/start", response_model=RunnerResponse)
    def start_deployment(request: StartDeploymentRequest) -> RunnerResponse:
        return _dispatch(service, request)

    @router.post("/deployments/stop", response_model=RunnerResponse)
    def stop_deployment(request: StopDeploymentRequest) -> RunnerResponse:
        return _dispatch(service, request)

    @router.get("/deployments/{deployment_id}", response_model=RunnerResponse)
    def get_deployment(
        deployment_id: str,
        envelope: Annotated[RunnerQueryEnvelope, Depends(_query_envelope)],
    ) -> RunnerResponse:
        try:
            request = DeploymentStatusRequest(
                **envelope.model_dump(),
                payload=DeploymentRefPayload(deployment_id=deployment_id),
            )
        except ValidationError as error:
            raise _invalid_request(INVALID_DEPLOYMENT_ID_MESSAGE) from error
        return _dispatch(service, request)

    @router.post("/benchmarks/start", response_model=RunnerResponse)
    def start_benchmark(request: StartBenchmarkRequest) -> RunnerResponse:
        return _dispatch(service, request)

    @router.post("/benchmarks/{benchmark_id}/cancel", response_model=RunnerResponse)
    def cancel_benchmark(
        benchmark_id: str,
        request: CancelBenchmarkRequest,
    ) -> RunnerResponse:
        expected = f"benchmark_{request.payload.job_id.removeprefix('job_')}"
        if benchmark_id != expected:
            raise _path_conflict()
        return _dispatch(service, request)

    @router.get("/jobs/{job_id}", response_model=RunnerResponse)
    def get_job(
        job_id: str,
        envelope: Annotated[RunnerQueryEnvelope, Depends(_query_envelope)],
    ) -> RunnerResponse:
        try:
            request = JobStatusRequest(
                **envelope.model_dump(),
                payload=JobRefPayload(job_id=job_id),
            )
        except ValidationError as error:
            raise _invalid_request(INVALID_JOB_ID_MESSAGE) from error
        return _dispatch(service, request)

    @router.get("/jobs/{job_id}/artifacts", response_model=RunnerResponse)
    def get_job_artifacts(
        job_id: str,
        envelope: Annotated[RunnerQueryEnvelope, Depends(_query_envelope)],
    ) -> RunnerResponse:
        try:
            request = JobArtifactsRequest(
                **envelope.model_dump(),
                payload=JobRefPayload(job_id=job_id),
            )
        except ValidationError as error:
            raise _invalid_request(INVALID_JOB_ID_MESSAGE) from error
        return _dispatch(service, request)

    app.include_router(router)
    return app
