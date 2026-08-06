"""FastAPI REST/SSE presentation boundary for the MVP control plane."""

import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Annotated, Literal, Self, cast

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
from autopilot.gateway.models import GatewayEnvironment
from autopilot.graph.agent import (
    AgentMessageView,
    AgentRunResult,
    AgentRuntimeError,
    AgentSessionPort,
    AgentStreamEvent,
    AgentToolCallView,
)
from autopilot.graph.state import GraphStateSnapshot, new_state
from autopilot.graph.workflow import GraphRunResult, GraphRuntime
from autopilot.jobs.ports import JobRepository


class CreateExperimentRequest(StrictModel):
    schema_version: str = "create-experiment-request/v1"
    message: LongText


class CreateSessionRequest(StrictModel):
    schema_version: str = "create-session-request/v1"
    message: LongText | None = None


class SessionMessageRequest(StrictModel):
    schema_version: str = "session-message-request/v1"
    message: LongText


class SessionResumeRequest(StrictModel):
    schema_version: str = "session-resume-request/v1"
    decision: Literal["approve", "reject"]
    message: LongText | None = None


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


class AgentSessionView(StrictModel):
    schema_version: str = "agent-session-view/v1"
    experiment_id: ExperimentId
    thread_id: NonEmptyStr
    status: ExperimentStatus
    phase: ExperimentPhase
    messages: tuple[AgentMessageView, ...]
    tool_calls: tuple[AgentToolCallView, ...] = ()
    interrupted: bool
    interrupt: dict[str, JsonValue] | None = None
    tool_set_id: NonEmptyStr | None = None
    tool_set_version: NonEmptyStr | None = None


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
    agent: AgentSessionPort | None = None
    agent_environment: Callable[[ExperimentId, HumanSubject], GatewayEnvironment] | None = None
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


def _default_agent_environment(
    experiment_id: ExperimentId,
    subject: HumanSubject,
) -> GatewayEnvironment:
    """Build trusted context; production may replace capability sets from its Collector."""
    return GatewayEnvironment(experiment_id=experiment_id, subject=subject)


def _agent_view(
    deps: ApiDependencies,
    experiment_id: ExperimentId,
    result: AgentRunResult,
) -> AgentSessionView:
    record = deps.experiments.get(experiment_id)
    if record is None:
        raise _api_error(404, "EXPERIMENT_NOT_FOUND", "Experiment does not exist")
    projection = dict(record.graph_state)
    projection["status"] = (
        ExperimentStatus.WAITING_APPROVAL.value
        if result.interrupted
        else ExperimentStatus.ACTIVE.value
    )
    if result.interrupt_payload is not None:
        current_phase = projection.get("phase", record.phase.value)
        if current_phase != ExperimentPhase.APPROVAL.value:
            projection["approval_resume_phase"] = current_phase
        projection["phase"] = ExperimentPhase.APPROVAL.value
        projection["approval_request"] = result.interrupt_payload
    else:
        projection.pop("approval_request", None)
        resume_phase = projection.pop("approval_resume_phase", None)
        if isinstance(resume_phase, str):
            projection["phase"] = resume_phase
    record = deps.experiments.save_state(
        experiment_id,
        projection,
    )
    return AgentSessionView(
        experiment_id=experiment_id,
        thread_id=str(experiment_id),
        status=record.status,
        phase=record.phase,
        messages=result.messages,
        tool_calls=result.tool_calls,
        interrupted=result.interrupted,
        interrupt=result.interrupt_payload,
        tool_set_id=result.tool_set_id,
        tool_set_version=result.tool_set_version,
    )


def _restore_session_phase(
    deps: ApiDependencies,
    experiment_id: ExperimentId,
    record: ExperimentRecord,
) -> ExperimentRecord:
    """Restore the pre-Interrupt phase before Gateway visibility is resolved."""
    resume_phase = record.graph_state.get("approval_resume_phase")
    if not isinstance(resume_phase, str) or resume_phase == ExperimentPhase.APPROVAL.value:
        return record
    try:
        phase = ExperimentPhase(resume_phase)
    except ValueError as error:
        raise _api_error(
            409,
            "SESSION_PHASE_INVALID",
            "Session approval phase is invalid",
        ) from error
    state = dict(record.graph_state)
    state["phase"] = phase.value
    state["status"] = ExperimentStatus.ACTIVE.value
    return deps.experiments.save_state(experiment_id, state)


def _stream_event_line(event_type: str, payload: Mapping[str, JsonValue]) -> str:
    return (
        f"event: {event_type}\n"
        f"data: {json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)}\n\n"
    )


def _agent_event_stream(
    deps: ApiDependencies,
    experiment_id: ExperimentId,
    events: Iterator[AgentStreamEvent],
) -> Iterator[str]:
    """Serialize Agent v2 events and persist the final session projection."""
    try:
        for event in events:
            payload = dict(event.payload)
            if event.result is not None:
                session = _agent_view(deps, experiment_id, event.result)
                payload["session"] = cast(dict[str, JsonValue], session.model_dump(mode="json"))
            yield _stream_event_line(event.event_type, payload)
    except AgentRuntimeError as error:
        yield _stream_event_line("run.error", {"code": error.code})
    yield _stream_event_line("stream.end", {})


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

    def require_agent() -> AgentSessionPort:
        if dependencies.agent is None:
            raise _api_error(503, "AGENT_UNAVAILABLE", "Agent runtime is not configured")
        return dependencies.agent

    def agent_environment(
        experiment_id: ExperimentId,
        subject: HumanSubject,
    ) -> GatewayEnvironment:
        factory = dependencies.agent_environment or _default_agent_environment
        return factory(experiment_id, subject)

    @app.post("/api/v1/sessions", response_model=AgentSessionView, status_code=201)
    def create_session(
        body: CreateSessionRequest,
        subject: Annotated[HumanSubject, Depends(authenticated)],
    ) -> AgentSessionView:
        agent = require_agent()
        experiment_id = ExperimentId.new()
        state = new_state(
            experiment_id=experiment_id,
            thread_id=str(experiment_id),
            message=body.message,
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
            if body.message is None:
                return AgentSessionView(
                    experiment_id=experiment_id,
                    thread_id=str(experiment_id),
                    status=record.status,
                    phase=record.phase,
                    messages=(),
                    interrupted=False,
                )
            result = agent.send(
                experiment_id=experiment_id,
                message=body.message,
                environment=agent_environment(experiment_id, subject),
            )
            return _agent_view(dependencies, experiment_id, result)
        except ValueError as error:
            raise _api_error(422, "VALIDATION_ERROR", str(error)) from error
        except AgentRuntimeError as error:
            raise _api_error(
                503, "AGENT_TURN_FAILED", "Agent turn could not be completed"
            ) from error

    @app.get("/api/v1/sessions/{experiment_id}", response_model=AgentSessionView)
    def get_session(
        experiment_id: str,
        subject: Annotated[HumanSubject, Depends(authenticated)],
    ) -> AgentSessionView:
        agent = require_agent()
        typed_experiment_id = _parse_stable_id(ExperimentId, experiment_id)
        record = experiment_or_404(typed_experiment_id)
        messages = agent.state(experiment_id=typed_experiment_id)
        interrupt_payload = record.graph_state.get("approval_request")
        return AgentSessionView(
            experiment_id=typed_experiment_id,
            thread_id=str(typed_experiment_id),
            status=record.status,
            phase=record.phase,
            messages=messages,
            interrupted=record.status is ExperimentStatus.WAITING_APPROVAL,
            interrupt=(interrupt_payload if isinstance(interrupt_payload, dict) else None),
        )

    @app.get("/api/v1/sessions/{experiment_id}/events")
    def session_events(
        experiment_id: str,
        subject: Annotated[HumanSubject, Depends(authenticated)],
    ) -> StreamingResponse:
        agent = require_agent()
        typed_experiment_id = _parse_stable_id(ExperimentId, experiment_id)
        record = experiment_or_404(typed_experiment_id)
        messages = agent.state(experiment_id=typed_experiment_id)
        interrupt_payload = record.graph_state.get("approval_request")

        def stream() -> Iterator[str]:
            for message in messages:
                payload = json.dumps(message.model_dump(mode="json"), ensure_ascii=False)
                yield f"event: agent.message\ndata: {payload}\n\n"
            if isinstance(interrupt_payload, dict):
                payload = json.dumps(interrupt_payload, ensure_ascii=False)
                yield f"event: agent.interrupt\ndata: {payload}\n\n"
            yield "event: stream.end\ndata: {}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post(
        "/api/v1/sessions/{experiment_id}/messages",
        response_model=AgentSessionView,
    )
    def send_session_message(
        experiment_id: str,
        body: SessionMessageRequest,
        subject: Annotated[HumanSubject, Depends(authenticated)],
    ) -> AgentSessionView:
        agent = require_agent()
        typed_experiment_id = _parse_stable_id(ExperimentId, experiment_id)
        record = experiment_or_404(typed_experiment_id)
        if record.status in {ExperimentStatus.COMPLETED, ExperimentStatus.CANCELLED}:
            raise _api_error(409, "SESSION_TERMINAL", "Session is already terminal")
        if record.status is ExperimentStatus.WAITING_APPROVAL:
            raise _api_error(
                409, "SESSION_WAITING_APPROVAL", "Session requires an approval decision"
            )
        try:
            result = agent.send(
                experiment_id=typed_experiment_id,
                message=body.message,
                environment=agent_environment(typed_experiment_id, subject),
            )
            return _agent_view(dependencies, typed_experiment_id, result)
        except RuntimeError as error:
            raise _api_error(
                409, "AGENT_TURN_FAILED", "Agent turn could not be completed"
            ) from error

    @app.post(
        "/api/v1/sessions/{experiment_id}/resume",
        response_model=AgentSessionView,
    )
    def resume_session(
        experiment_id: str,
        body: SessionResumeRequest,
        subject: Annotated[HumanSubject, Depends(authenticated)],
    ) -> AgentSessionView:
        agent = require_agent()
        typed_experiment_id = _parse_stable_id(ExperimentId, experiment_id)
        record = experiment_or_404(typed_experiment_id)
        if record.status is not ExperimentStatus.WAITING_APPROVAL:
            raise _api_error(409, "SESSION_NOT_INTERRUPTED", "Session has no pending approval")
        _require_admin(subject)
        if subject.user_id == record.created_by:
            raise _api_error(
                403,
                "APPROVAL_SELF_DECISION",
                "Requester cannot approve its own session",
            )
        record = _restore_session_phase(dependencies, typed_experiment_id, record)
        try:
            result = agent.resume(
                experiment_id=typed_experiment_id,
                approved=body.decision == "approve",
                message=body.message,
                environment=agent_environment(typed_experiment_id, subject),
            )
            return _agent_view(dependencies, typed_experiment_id, result)
        except RuntimeError as error:
            raise _api_error(
                409, "AGENT_RESUME_FAILED", "Agent Interrupt could not be resumed"
            ) from error

    @app.post("/api/v1/sessions/{experiment_id}/messages/stream")
    def stream_session_message(
        experiment_id: str,
        body: SessionMessageRequest,
        subject: Annotated[HumanSubject, Depends(authenticated)],
    ) -> StreamingResponse:
        agent = require_agent()
        stream_send = getattr(agent, "stream_send", None)
        if not callable(stream_send):
            raise _api_error(503, "AGENT_STREAM_UNAVAILABLE", "Agent streaming is not configured")
        typed_experiment_id = _parse_stable_id(ExperimentId, experiment_id)
        record = experiment_or_404(typed_experiment_id)
        if record.status in {ExperimentStatus.COMPLETED, ExperimentStatus.CANCELLED}:
            raise _api_error(409, "SESSION_TERMINAL", "Session is already terminal")
        if record.status is ExperimentStatus.WAITING_APPROVAL:
            raise _api_error(
                409,
                "SESSION_WAITING_APPROVAL",
                "Session requires an approval decision",
            )
        events = stream_send(
            experiment_id=typed_experiment_id,
            message=body.message,
            environment=agent_environment(typed_experiment_id, subject),
        )
        return StreamingResponse(
            _agent_event_stream(dependencies, typed_experiment_id, events),
            media_type="text/event-stream",
        )

    @app.post("/api/v1/sessions/{experiment_id}/resume/stream")
    def stream_session_resume(
        experiment_id: str,
        body: SessionResumeRequest,
        subject: Annotated[HumanSubject, Depends(authenticated)],
    ) -> StreamingResponse:
        agent = require_agent()
        stream_resume = getattr(agent, "stream_resume", None)
        if not callable(stream_resume):
            raise _api_error(503, "AGENT_STREAM_UNAVAILABLE", "Agent streaming is not configured")
        typed_experiment_id = _parse_stable_id(ExperimentId, experiment_id)
        record = experiment_or_404(typed_experiment_id)
        if record.status is not ExperimentStatus.WAITING_APPROVAL:
            raise _api_error(409, "SESSION_NOT_INTERRUPTED", "Session has no pending approval")
        _require_admin(subject)
        if subject.user_id == record.created_by:
            raise _api_error(
                403,
                "APPROVAL_SELF_DECISION",
                "Requester cannot approve its own session",
            )
        record = _restore_session_phase(dependencies, typed_experiment_id, record)
        events = stream_resume(
            experiment_id=typed_experiment_id,
            approved=body.decision == "approve",
            message=body.message,
            environment=agent_environment(typed_experiment_id, subject),
        )
        return StreamingResponse(
            _agent_event_stream(dependencies, typed_experiment_id, events),
            media_type="text/event-stream",
        )

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
