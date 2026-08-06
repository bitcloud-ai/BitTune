from datetime import UTC, datetime
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from autopilot.domain.base import SchemaVersion, StrictModel
from autopilot.domain.enums import ExperimentPhase, RiskLevel, UserRole
from autopilot.domain.identifiers import (
    ExperimentId,
    ToolName,
    UserId,
)
from autopilot.domain.identities import HumanSubject
from autopilot.domain.plans import PlanExecutionRequest
from autopilot.gateway.models import (
    GatewayEnvironment,
    ToolCallRequest,
    ToolDefinition,
    ToolExecutionMode,
    ToolSetEntry,
    ToolSetSnapshot,
    VisibilityContext,
    create_toolset_snapshot,
)
from autopilot.gateway.registry import ToolRegistration
from autopilot.graph.agent import AgentRuntime


class QueryInput(StrictModel):
    schema_version: Literal["query-input/v1"] = "query-input/v1"


class ToolResult(StrictModel):
    schema_version: Literal["tool-result/v1"] = "tool-result/v1"
    status: Literal["ready"] = "ready"


class ScriptedToolModel(BaseChatModel):
    tool_name: str
    final_answer: str = "completed through the gateway"
    bound_tool_names: tuple[str, ...] = ()

    @property
    def _llm_type(self) -> str:
        return "scripted-tool-model"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        del tool_choice, kwargs
        names = tuple(tool.name for tool in tools)
        return self.model_copy(update={"bound_tool_names": names})

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager=None,
        **kwargs,
    ) -> ChatResult:
        del stop, run_manager, kwargs
        if any(isinstance(message, ToolMessage) for message in messages):
            answer = AIMessage(content=self.final_answer)
        elif self.tool_name in self.bound_tool_names:
            args = (
                {
                    "schema_version": "plan-execution-request/v1",
                    "plan_id": "plan_" + "1" * 32,
                    "expected_plan_hash": "sha256:" + "2" * 64,
                }
                if self.tool_name.startswith("start_")
                else {"schema_version": "query-input/v1"}
            )
            answer = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": self.tool_name,
                        "args": args,
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            )
        else:
            answer = AIMessage(content="no tools are currently available")
        return ChatResult(generations=[ChatGeneration(message=answer)])


class RecordingGateway:
    def __init__(self, registration: ToolRegistration, environment: GatewayEnvironment) -> None:
        self.registration = registration
        self.environment = environment
        self.requests: list[ToolCallRequest] = []

    def available_tools(
        self,
        *,
        environment: GatewayEnvironment,
        request_id: str,
    ) -> ToolSetSnapshot:
        assert environment == self.environment
        assert request_id.startswith("agent-turn-")
        definition = self.registration.definition
        return create_toolset_snapshot(
            context=VisibilityContext(
                experiment_id=environment.experiment_id,
                subject=environment.subject,
                phase=ExperimentPhase.ENVIRONMENT,
            ),
            tools=(
                ToolSetEntry(
                    name=definition.name,
                    schema_version=definition.input_schema_version,
                    risk_level=definition.risk_level,
                ),
            ),
            policy_decision_ids=("decision-1",),
            created_at=datetime(2026, 8, 6, tzinfo=UTC),
        )

    def invoke(
        self,
        *,
        request: ToolCallRequest,
        environment: GatewayEnvironment,
    ) -> ToolResult:
        assert environment == self.environment
        self.requests.append(request)
        return ToolResult()


def _environment(experiment_id: ExperimentId) -> GatewayEnvironment:
    return GatewayEnvironment(
        experiment_id=experiment_id,
        subject=HumanSubject(
            user_id=UserId(root="user_" + "3" * 32),
            role=UserRole.ADMIN,
        ),
    )


def _registration(*, tool_name: str, risk: RiskLevel) -> ToolRegistration:
    input_model = PlanExecutionRequest if risk is RiskLevel.L2 else QueryInput
    return ToolRegistration(
        definition=ToolDefinition(
            name=ToolName(root=tool_name),
            input_schema_version=SchemaVersion(
                "plan-execution-request/v1" if risk is RiskLevel.L2 else "query-input/v1"
            ),
            risk_level=risk,
            execution_mode=(
                ToolExecutionMode.ASYNC_JOB if risk is RiskLevel.L2 else ToolExecutionMode.READ_ONLY
            ),
            allowed_phases=(ExperimentPhase.ENVIRONMENT,),
            allowed_roles=(UserRole.ADMIN,),
            requires_plan=risk is RiskLevel.L2,
        ),
        input_model=input_model,
    )


def test_agent_model_selects_a_visible_tool_and_only_executes_through_gateway() -> None:
    experiment_id = ExperimentId.new()
    environment = _environment(experiment_id)
    registration = _registration(tool_name="get_environment_status", risk=RiskLevel.L0)
    gateway = RecordingGateway(registration, environment)
    runtime = AgentRuntime(
        model=ScriptedToolModel(tool_name="get_environment_status"),
        gateway=gateway,
        registrations=(registration,),
    )

    result = runtime.send(
        experiment_id=experiment_id,
        message="check the environment status",
        environment=environment,
    )

    assert not result.interrupted
    assert result.assistant_message == "completed through the gateway"
    assert [request.tool_name for request in gateway.requests] == [
        ToolName(root="get_environment_status")
    ]
    assert result.tool_set_version is not None


def test_l2_tool_interrupts_before_gateway_and_resumes_with_official_hitl() -> None:
    experiment_id = ExperimentId.new()
    environment = _environment(experiment_id)
    registration = _registration(tool_name="start_deployment", risk=RiskLevel.L2)
    gateway = RecordingGateway(registration, environment)
    runtime = AgentRuntime(
        model=ScriptedToolModel(tool_name="start_deployment"),
        gateway=gateway,
        registrations=(registration,),
    )

    interrupted = runtime.send(
        experiment_id=experiment_id,
        message="deploy the approved plan",
        environment=environment,
    )

    assert interrupted.interrupted
    assert gateway.requests == []
    assert interrupted.interrupt_payload is not None
    assert interrupted.interrupt_payload["action_requests"][0]["name"] == "start_deployment"

    completed = runtime.resume(
        experiment_id=experiment_id,
        approved=True,
        environment=environment,
    )

    assert not completed.interrupted
    assert completed.assistant_message == "completed through the gateway"
    assert len(gateway.requests) == 1
    assert gateway.requests[0].arguments["plan_id"] == "plan_" + "1" * 32
    assert gateway.requests[0].arguments["expected_plan_hash"] == "sha256:" + "2" * 64


def test_same_thread_keeps_conversation_history_across_turns() -> None:
    experiment_id = ExperimentId.new()
    environment = _environment(experiment_id)
    registration = _registration(tool_name="get_environment_status", risk=RiskLevel.L0)
    gateway = RecordingGateway(registration, environment)
    runtime = AgentRuntime(
        model=ScriptedToolModel(tool_name="get_environment_status"),
        gateway=gateway,
        registrations=(),
    )

    runtime.send(
        experiment_id=experiment_id,
        message="first question",
        environment=environment,
    )
    runtime.send(
        experiment_id=experiment_id,
        message="second question",
        environment=environment,
    )

    user_messages = [
        message.content
        for message in runtime.state(experiment_id=experiment_id)
        if message.role == "user"
    ]
    assert user_messages == ["first question", "second question"]
