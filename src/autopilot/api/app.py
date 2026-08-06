"""FastAPI REST/SSE presentation boundary for the MVP control plane."""

import json
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Annotated, Self, cast

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import JsonValue, SecretStr, model_validator
from starlette.types import Lifespan

from autopilot.api.repositories import (
    ApprovalStore,
    ArtifactQuery,
    DeploymentProjection,
    DeploymentStore,
    ExperimentRecord,
    ExperimentStore,
    PlanProjection,
    PlanStore,
)
from autopilot.domain.base import LongText, NonEmptyStr, StrictModel, utc_now
from autopilot.domain.enums import ApprovalDecision, ExperimentPhase, ExperimentStatus, UserRole
from autopilot.domain.identifiers import (
    ApprovalId,
    ArtifactId,
    DeploymentId,
    ExperimentId,
    JobId,
    PlanHash,
    PlanId,
    StableId,
    ToolName,
)
from autopilot.domain.identities import HumanSubject
from autopilot.domain.jobs import JobRecord
from autopilot.gateway.approval_ports import DecideApprovalRequest
from autopilot.gateway.authentication import AuthenticationError, BearerTokenAuthenticator
from autopilot.graph.state import GraphStateSnapshot, new_state
from autopilot.graph.workflow import GraphRunResult, GraphRuntime
from autopilot.jobs.ports import JobRepository


class CreateExperimentRequest(StrictModel):
    schema_version: str = "create-experiment-request/v1"
    message: LongText


class ExperimentMessageRequest(StrictModel):
    schema_version: str = "experiment-message-request/v1"
    message: LongText


class ResumeRequest(StrictModel):
    schema_version: str = "experiment-resume-request/v1"
    decision: ApprovalDecision | None = None
    message: LongText | None = None
    comment: LongText | None = None

    @model_validator(mode="after")
    def validate_answer(self) -> Self:
        if self.decision is None and self.message is None:
            raise ValueError("resume requires decision or message")
        if self.decision is not None and self.message is not None:
            raise ValueError("resume accepts one answer form")
        if self.decision is ApprovalDecision.PENDING or self.decision is ApprovalDecision.EXPIRED:
            raise ValueError("resume decision must be approved or rejected")
        return self


class PlanDecisionRequest(StrictModel):
    schema_version: str = "plan-decision-request/v1"
    approval_id: ApprovalId
    expected_plan_hash: PlanHash
    action: ToolName
    comment: LongText | None = None
    decision: ApprovalDecision

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if self.decision not in {ApprovalDecision.APPROVED, ApprovalDecision.REJECTED}:
            raise ValueError("plan decision must be approved or rejected")
        return self


class ExperimentView(StrictModel):
    schema_version: str = "experiment-view/v1"
    experiment_id: ExperimentId
    status: ExperimentStatus
    phase: ExperimentPhase
    state: GraphStateSnapshot
    created_at: str
    updated_at: str


class GraphRunView(StrictModel):
    schema_version: str = "graph-run-view/v1"
    experiment: ExperimentView
    interrupted: bool
    interrupt: dict[str, JsonValue] | None = None


class JobView(StrictModel):
    schema_version: str = "job-view/v1"
    job: JobRecord


class ArtifactDownloadMeta(StrictModel):
    schema_version: str = "artifact-download-meta/v1"
    artifact_id: ArtifactId
    content_type: NonEmptyStr
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ApiDependencies:
    authenticator: BearerTokenAuthenticator
    experiments: ExperimentStore
    graph: GraphRuntime
    plans: PlanStore | None = None
    jobs: JobRepository | None = None
    approvals: ApprovalStore | None = None
    deployments: DeploymentStore | None = None
    artifacts: ArtifactQuery | None = None


def _api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"schema_version": "error-envelope/v1", "code": code, "message": message},
    )


def _parse_stable_id[StableIdT: StableId](model: type[StableIdT], value: str) -> StableIdT:
    try:
        return model(root=value)
    except ValueError as error:
        raise _api_error(422, "INVALID_RESOURCE_ID", "resource ID is invalid") from error


def _subject_from_header(
    deps: ApiDependencies,
    authorization: str | None,
) -> HumanSubject:
    if authorization is None or not authorization.startswith("Bearer "):
        raise _api_error(401, "AUTHENTICATION_FAILED", "authentication failed")
    token = authorization.removeprefix("Bearer ")
    if not token or " " in token:
        raise _api_error(401, "AUTHENTICATION_FAILED", "authentication failed")
    try:
        return deps.authenticator.authenticate(SecretStr(token))
    except AuthenticationError as error:
        raise _api_error(401, "AUTHENTICATION_FAILED", "authentication failed") from error


def _require_admin(subject: HumanSubject) -> None:
    if subject.role is not UserRole.ADMIN:
        raise _api_error(403, "FORBIDDEN", "human admin role is required")


def _experiment_view(record: ExperimentRecord) -> ExperimentView:
    return ExperimentView(
        experiment_id=record.experiment_id,
        status=record.status,
        phase=record.phase,
        state=GraphStateSnapshot.model_validate(record.graph_state),
        created_at=record.created_at.isoformat(),
        updated_at=record.updated_at.isoformat(),
    )


def _run_view(deps: ApiDependencies, result: GraphRunResult) -> GraphRunView:
    record = deps.experiments.save_state(
        result.state.experiment_id,
        cast(dict[str, JsonValue], result.state.model_dump(mode="json")),
    )
    return GraphRunView(
        experiment=_experiment_view(record),
        interrupted=result.interrupted,
        interrupt=result.interrupt_payload,
    )


def create_app(
    dependencies: ApiDependencies,
    *,
    lifespan: Lifespan[FastAPI] | None = None,
) -> FastAPI:
    """Create an explicitly wired app; production never falls back to memory state."""
    app = FastAPI(
        title="LLM Inference Autopilot MVP",
        version="0.1.0",
        description="Auditable single-GPU inference workflow control plane",
        lifespan=lifespan,
    )

    def authenticated(
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> HumanSubject:
        return _subject_from_header(dependencies, authorization)

    def experiment_or_404(experiment_id: ExperimentId) -> ExperimentRecord:
        record = dependencies.experiments.get(experiment_id)
        if record is None:
            raise _api_error(404, "EXPERIMENT_NOT_FOUND", "Experiment does not exist")
        return record

    @app.get("/healthz", include_in_schema=True)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/experiments", response_model=GraphRunView, status_code=201)
    def create_experiment(
        body: CreateExperimentRequest,
        subject: Annotated[HumanSubject, Depends(authenticated)],
    ) -> GraphRunView:
        experiment_id = ExperimentId.new()
        state = new_state(
            experiment_id=experiment_id, thread_id=str(experiment_id), message=body.message
        )
        record = ExperimentRecord(
            experiment_id=experiment_id,
            created_by=subject.user_id,
            status=ExperimentStatus.ACTIVE,
            phase=ExperimentPhase.REQUIREMENTS,
            graph_state=cast(dict[str, JsonValue], state),
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        try:
            dependencies.experiments.create(record)
            result = dependencies.graph.start(experiment_id=experiment_id, state=state)
            return _run_view(dependencies, result)
        except ValueError as error:
            raise _api_error(422, "VALIDATION_ERROR", str(error)) from error

    @app.get("/api/v1/experiments/{experiment_id}", response_model=ExperimentView)
    def get_experiment(
        experiment_id: str,
        subject: Annotated[HumanSubject, Depends(authenticated)],
    ) -> ExperimentView:
        return _experiment_view(experiment_or_404(_parse_stable_id(ExperimentId, experiment_id)))

    @app.post("/api/v1/experiments/{experiment_id}/messages", response_model=GraphRunView)
    def send_message(
        experiment_id: str,
        body: ExperimentMessageRequest,
        subject: Annotated[HumanSubject, Depends(authenticated)],
    ) -> GraphRunView:
        typed_experiment_id = _parse_stable_id(ExperimentId, experiment_id)
        record = experiment_or_404(typed_experiment_id)
        if record.status in {ExperimentStatus.COMPLETED, ExperimentStatus.CANCELLED}:
            raise _api_error(409, "EXPERIMENT_TERMINAL", "Experiment is already terminal")
        try:
            result = dependencies.graph.resume(
                experiment_id=typed_experiment_id,
                answer={"message": body.message},
            )
        except RuntimeError as error:
            raise _api_error(
                409, "GRAPH_NOT_WAITING", "Experiment is not waiting for a message"
            ) from error
        return _run_view(dependencies, result)

    @app.post("/api/v1/experiments/{experiment_id}/resume", response_model=GraphRunView)
    def resume_experiment(
        experiment_id: str,
        body: ResumeRequest,
        subject: Annotated[HumanSubject, Depends(authenticated)],
    ) -> GraphRunView:
        typed_experiment_id = _parse_stable_id(ExperimentId, experiment_id)
        experiment_or_404(typed_experiment_id)
        answer: dict[str, JsonValue]
        if body.message is not None:
            answer = {"message": body.message}
        else:
            decision = body.decision
            if decision is None:
                raise _api_error(422, "VALIDATION_ERROR", "resume decision is required")
            answer = {"decision": decision.value, "comment": body.comment or ""}
        try:
            result = dependencies.graph.resume(experiment_id=typed_experiment_id, answer=answer)
        except RuntimeError as error:
            raise _api_error(
                409, "GRAPH_NOT_INTERRUPTED", "Experiment has no resumable interrupt"
            ) from error
        return _run_view(dependencies, result)

    @app.post("/api/v1/experiments/{experiment_id}/cancel", response_model=ExperimentView)
    def cancel_experiment(
        experiment_id: str,
        subject: Annotated[HumanSubject, Depends(authenticated)],
    ) -> ExperimentView:
        typed_experiment_id = _parse_stable_id(ExperimentId, experiment_id)
        record = experiment_or_404(typed_experiment_id)
        cancelled = dependencies.experiments.cancel(typed_experiment_id)
        active_job_id = record.graph_state.get("active_job_id")
        if dependencies.jobs is not None and isinstance(active_job_id, str):
            with suppress(KeyError, ValueError, RuntimeError):
                dependencies.jobs.request_cancel(job_id=JobId(root=active_job_id))
        return _experiment_view(cancelled)

    @app.get("/api/v1/experiments/{experiment_id}/events")
    def experiment_events(
        experiment_id: str,
        subject: Annotated[HumanSubject, Depends(authenticated)],
    ) -> StreamingResponse:
        record = experiment_or_404(_parse_stable_id(ExperimentId, experiment_id))

        def stream() -> Iterator[str]:
            payload = json.dumps(
                _experiment_view(record).model_dump(mode="json"), ensure_ascii=False
            )
            yield f"event: graph.state\ndata: {payload}\n\n"
            yield "event: stream.end\ndata: {}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/v1/jobs/{job_id}", response_model=JobView)
    def get_job(
        job_id: str,
        subject: Annotated[HumanSubject, Depends(authenticated)],
    ) -> JobView:
        if dependencies.jobs is None:
            raise _api_error(503, "JOB_STORE_UNAVAILABLE", "Job store is not configured")
        job = dependencies.jobs.get(_parse_stable_id(JobId, job_id))
        if job is None:
            raise _api_error(404, "JOB_NOT_FOUND", "Job does not exist")
        return JobView(job=job)

    @app.post("/api/v1/jobs/{job_id}/cancel", response_model=JobView)
    def cancel_job(
        job_id: str,
        subject: Annotated[HumanSubject, Depends(authenticated)],
    ) -> JobView:
        if dependencies.jobs is None:
            raise _api_error(503, "JOB_STORE_UNAVAILABLE", "Job store is not configured")
        try:
            return JobView(
                job=dependencies.jobs.request_cancel(job_id=_parse_stable_id(JobId, job_id))
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise _api_error(404, "JOB_NOT_FOUND", "Job does not exist") from error

    @app.get("/api/v1/jobs/{job_id}/logs")
    def get_job_logs(
        job_id: str,
        subject: Annotated[HumanSubject, Depends(authenticated)],
    ) -> dict[str, JsonValue]:
        typed_job_id = _parse_stable_id(JobId, job_id)
        if dependencies.jobs is None or dependencies.jobs.get(typed_job_id) is None:
            raise _api_error(404, "JOB_NOT_FOUND", "Job does not exist")
        return {"schema_version": "job-logs-v1", "artifact_ref": None}

    @app.get("/api/v1/jobs/{job_id}/result")
    def get_job_result(
        job_id: str,
        subject: Annotated[HumanSubject, Depends(authenticated)],
    ) -> dict[str, JsonValue]:
        if dependencies.jobs is None:
            raise _api_error(503, "JOB_STORE_UNAVAILABLE", "Job store is not configured")
        job = dependencies.jobs.get(_parse_stable_id(JobId, job_id))
        if job is None:
            raise _api_error(404, "JOB_NOT_FOUND", "Job does not exist")
        if job.result_artifact is None:
            raise _api_error(409, "JOB_RESULT_UNAVAILABLE", "Job has no completed result")
        return {
            "schema_version": "job-result-v1",
            "artifact_ref": cast(JsonValue, job.result_artifact.model_dump(mode="json")),
        }

    @app.get("/api/v1/plans/{plan_id}", response_model=PlanProjection)
    def get_plan(
        plan_id: str,
        experiment_id: Annotated[str, Header(alias="X-Experiment-ID")],
        subject: Annotated[HumanSubject, Depends(authenticated)],
    ) -> PlanProjection:
        if dependencies.plans is None:
            raise _api_error(503, "PLAN_STORE_UNAVAILABLE", "Plan store is not configured")
        plan = dependencies.plans.get(
            _parse_stable_id(ExperimentId, experiment_id),
            _parse_stable_id(PlanId, plan_id),
        )
        if plan is None:
            raise _api_error(404, "PLAN_NOT_FOUND", "Plan does not exist")
        return plan

    @app.post("/api/v1/plans/{plan_id}/approve", response_model=dict[str, JsonValue])
    @app.post("/api/v1/plans/{plan_id}/reject", response_model=dict[str, JsonValue])
    def decide_plan(
        plan_id: str,
        body: PlanDecisionRequest,
        experiment_id: Annotated[str, Header(alias="X-Experiment-ID")],
        subject: Annotated[HumanSubject, Depends(authenticated)],
    ) -> dict[str, JsonValue]:
        _require_admin(subject)
        if dependencies.approvals is None:
            raise _api_error(503, "APPROVAL_STORE_UNAVAILABLE", "Approval store is not configured")
        if body.decision is ApprovalDecision.APPROVED and body.action == "":
            raise _api_error(422, "VALIDATION_ERROR", "approval action is required")
        try:
            record = dependencies.approvals.decide(
                DecideApprovalRequest(
                    approval_id=body.approval_id,
                    experiment_id=_parse_stable_id(ExperimentId, experiment_id),
                    expected_plan_id=_parse_stable_id(PlanId, plan_id),
                    expected_plan_hash=body.expected_plan_hash,
                    expected_action=body.action,
                    actor=subject,
                    decision=body.decision,
                    comment=body.comment,
                )
            )
        except (ValueError, RuntimeError) as error:
            raise _api_error(
                409, "APPROVAL_DECISION_REJECTED", "Approval decision was rejected"
            ) from error
        return cast(dict[str, JsonValue], record.model_dump(mode="json"))

    @app.get("/api/v1/deployments/{deployment_id}", response_model=DeploymentProjection)
    def get_deployment(
        deployment_id: str,
        experiment_id: Annotated[str, Header(alias="X-Experiment-ID")],
        subject: Annotated[HumanSubject, Depends(authenticated)],
    ) -> DeploymentProjection:
        if dependencies.deployments is None:
            raise _api_error(
                503, "DEPLOYMENT_STORE_UNAVAILABLE", "Deployment store is not configured"
            )
        deployment = dependencies.deployments.get(
            _parse_stable_id(ExperimentId, experiment_id),
            _parse_stable_id(DeploymentId, deployment_id),
        )
        if deployment is None:
            raise _api_error(404, "DEPLOYMENT_NOT_FOUND", "Deployment does not exist")
        return deployment

    @app.get("/api/v1/artifacts/{artifact_id}/metadata")
    def artifact_metadata(
        artifact_id: str,
        experiment_id: Annotated[str, Header(alias="X-Experiment-ID")],
        subject: Annotated[HumanSubject, Depends(authenticated)],
    ) -> dict[str, JsonValue]:
        if dependencies.artifacts is None:
            raise _api_error(503, "ARTIFACT_STORE_UNAVAILABLE", "Artifact store is not configured")
        typed_experiment_id = _parse_stable_id(ExperimentId, experiment_id)
        typed_artifact_id = _parse_stable_id(ArtifactId, artifact_id)
        metadata = dependencies.artifacts.metadata(typed_experiment_id, str(typed_artifact_id))
        if metadata is None:
            raise _api_error(404, "ARTIFACT_NOT_FOUND", "Artifact does not exist")
        return cast(dict[str, JsonValue], metadata.model_dump(mode="json"))

    @app.get("/api/v1/artifacts/{artifact_id}/download")
    def download_artifact(
        artifact_id: str,
        experiment_id: Annotated[str, Header(alias="X-Experiment-ID")],
        subject: Annotated[HumanSubject, Depends(authenticated)],
    ) -> StreamingResponse:
        if dependencies.artifacts is None:
            raise _api_error(503, "ARTIFACT_STORE_UNAVAILABLE", "Artifact store is not configured")
        typed_experiment_id = _parse_stable_id(ExperimentId, experiment_id)
        typed_artifact_id = _parse_stable_id(ArtifactId, artifact_id)
        metadata = dependencies.artifacts.metadata(typed_experiment_id, str(typed_artifact_id))
        if metadata is None:
            raise _api_error(404, "ARTIFACT_NOT_FOUND", "Artifact does not exist")
        payload = dependencies.artifacts.read(typed_experiment_id, metadata)
        return StreamingResponse(iter((payload,)), media_type=metadata.content_type)

    return app


__all__ = [
    "ApiDependencies",
    "CreateExperimentRequest",
    "ExperimentMessageRequest",
    "ResumeRequest",
    "create_app",
]
