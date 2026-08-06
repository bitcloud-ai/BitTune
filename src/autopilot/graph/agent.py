"""LangChain Agent harness backed by the mandatory Autopilot Tool Gateway."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NotRequired, Protocol, TypedDict, cast
from uuid import uuid4

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import AgentMiddleware, HumanInTheLoopMiddleware
from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain.messages import AIMessage, ToolMessage
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import get_runtime
from langgraph.types import Command
from openai import APIError, APITimeoutError
from pydantic import BaseModel, JsonValue

from autopilot.domain.base import LongText, NonEmptyStr, StrictModel
from autopilot.domain.enums import RiskLevel
from autopilot.domain.identifiers import ExperimentId, ToolName
from autopilot.gateway.models import GatewayEnvironment, ToolCallRequest, ToolSetSnapshot
from autopilot.gateway.registry import ToolRegistration

SYSTEM_PROMPT = """You are BitTune, an LLM inference Autopilot for one Linux host and one RTX 5090.
Help the operator inspect the environment, plan capacity, deploy vLLM, benchmark with EvalScope,
optimize with Optuna, verify candidates, and explain the evidence. Use only the tools supplied in
the current turn. Never invent measured metrics, provider results, approvals, resource state, or
artifact references. Never request or emit credentials, shell commands, raw Docker options,
arbitrary paths, or provider CLI flags. Long-running actions return a job ID; use status and result
tools instead of waiting. Explain any approval request before execution."""
AGENT_ENVIRONMENT_MISMATCH = "Agent environment does not match the Experiment"
MESSAGE_STREAM_PART_SIZE = 2

_BEARER_SECRET = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/]+=*")
_NAMED_SECRET = re.compile(r"(?i)\b(api[_-]?key|authorization|password|token)\s*[:=]\s*([^\s,;]+)")


class AgentGateway(Protocol):
    """The only execution boundary available to Agent tools."""

    def available_tools(
        self,
        *,
        environment: GatewayEnvironment,
        request_id: str,
    ) -> ToolSetSnapshot: ...

    def invoke(
        self,
        *,
        request: ToolCallRequest,
        environment: GatewayEnvironment,
    ) -> BaseModel: ...


class AgentRuntimeError(RuntimeError):
    """Safe, classified failure at the remote model/checkpoint boundary."""

    def __init__(self, code: str = "AGENT_RUNTIME_FAILED") -> None:
        super().__init__(code)
        self.code = code


class AgentSessionPort(Protocol):
    """API-facing subset of the standard Agent runtime."""

    def send(
        self,
        *,
        experiment_id: ExperimentId,
        message: str,
        environment: GatewayEnvironment,
    ) -> AgentRunResult: ...

    def resume(
        self,
        *,
        experiment_id: ExperimentId,
        approved: bool,
        environment: GatewayEnvironment,
        message: str | None = None,
    ) -> AgentRunResult: ...

    def state(self, *, experiment_id: ExperimentId) -> tuple[AgentMessageView, ...]: ...

    def stream_send(
        self,
        *,
        experiment_id: ExperimentId,
        message: str,
        environment: GatewayEnvironment,
    ) -> Iterator[AgentStreamEvent]: ...

    def stream_resume(
        self,
        *,
        experiment_id: ExperimentId,
        approved: bool,
        environment: GatewayEnvironment,
        message: str | None = None,
    ) -> Iterator[AgentStreamEvent]: ...


class AgentInvocationContext(TypedDict):
    """Ephemeral trusted context; never persisted in Agent State."""

    gateway: AgentGateway
    environment: GatewayEnvironment
    request_id: str
    tool_set: NotRequired[ToolSetSnapshot]


class AgentMessageView(StrictModel):
    """Bounded, JSON-safe message projection returned by the API."""

    schema_version: str = "agent-message-view/v1"
    role: NonEmptyStr
    content: LongText
    tool_name: NonEmptyStr | None = None
    tool_call_id: NonEmptyStr | None = None


class AgentToolCallView(StrictModel):
    """Tool-call projection suitable for CLI event rendering."""

    schema_version: str = "agent-tool-call-view/v1"
    tool_name: ToolName
    arguments: dict[str, JsonValue]
    tool_call_id: NonEmptyStr


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """One completed or interrupted Agent turn."""

    messages: tuple[AgentMessageView, ...]
    tool_calls: tuple[AgentToolCallView, ...]
    interrupted: bool
    interrupt_payload: dict[str, JsonValue] | None
    tool_set_id: str | None
    tool_set_version: str | None

    @property
    def assistant_message(self) -> str | None:
        for message in reversed(self.messages):
            if message.role == "assistant":
                return str(message.content)
        return None


@dataclass(frozen=True, slots=True)
class AgentStreamEvent:
    """One bounded event emitted by the official Agent v2 stream."""

    event_type: str
    payload: dict[str, JsonValue]
    result: AgentRunResult | None = None


def redact_conversation_text(value: str) -> str:
    """Remove common credential forms before a message enters checkpointed State."""
    redacted = _BEARER_SECRET.sub("Bearer [REDACTED]", value)
    return _NAMED_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)


def _message_text(message: BaseMessage) -> str:
    text = cast(str, message.text)
    if text:
        return redact_conversation_text(text)
    return "[structured message]"


def _message_role(message: BaseMessage) -> str:
    if isinstance(message, AIMessage):
        return "assistant"
    if isinstance(message, ToolMessage):
        return "tool"
    if message.type == "human":
        return "user"
    return message.type


def _message_view(message: BaseMessage) -> AgentMessageView:
    return AgentMessageView(
        role=_message_role(message),
        content=_message_text(message),
        tool_name=message.name if isinstance(message, ToolMessage) else None,
        tool_call_id=message.tool_call_id if isinstance(message, ToolMessage) else None,
    )


def _tool_calls(messages: Sequence[BaseMessage]) -> tuple[AgentToolCallView, ...]:
    views: list[AgentToolCallView] = []
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls:
            arguments = call["args"]
            views.append(
                AgentToolCallView(
                    tool_name=ToolName(root=call["name"]),
                    arguments=arguments,
                    tool_call_id=call["id"],
                )
            )
    return tuple(views)


def _interrupt_value(output: Mapping[str, object]) -> dict[str, JsonValue] | None:
    interrupts = output.get("__interrupt__")
    if not isinstance(interrupts, (list, tuple)) or not interrupts:
        return None
    value = getattr(interrupts[0], "value", None)
    if isinstance(value, BaseModel):
        return cast(dict[str, JsonValue], value.model_dump(mode="json"))
    if isinstance(value, dict):
        return cast(dict[str, JsonValue], value)
    return None


def _tool_description(registration: ToolRegistration) -> str:
    definition = registration.definition
    effect = (
        "Starts or cancels an asynchronous job and may require human approval."
        if definition.risk_level is RiskLevel.L2
        else "Performs a read-only domain action."
    )
    return (
        f"Autopilot domain action {definition.name}. {effect} "
        "The Tool Gateway enforces schema, workflow, budget, policy, idempotency, and audit."
    )


def _gateway_tool(registration: ToolRegistration) -> BaseTool:
    definition = registration.definition

    def invoke_gateway(**arguments: JsonValue) -> str:
        context = get_runtime(AgentInvocationContext).context
        validated = registration.input_model.model_validate(arguments)
        serialized_arguments = cast(dict[str, JsonValue], validated.model_dump(mode="json"))
        snapshot = context.get("tool_set")
        if snapshot is None:
            snapshot = context["gateway"].available_tools(
                environment=context["environment"],
                request_id=context["request_id"],
            )
            context["tool_set"] = snapshot
        request = ToolCallRequest(
            request_id=f"agent-tool-{uuid4().hex}",
            tool_name=definition.name,
            tool_set_id=snapshot.tool_set_id,
            expected_tool_set_version=snapshot.tool_set_version,
            arguments=serialized_arguments,
        )
        result = context["gateway"].invoke(
            request=request,
            environment=context["environment"],
        )
        return json.dumps(result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)

    return StructuredTool.from_function(
        invoke_gateway,
        name=str(definition.name),
        description=_tool_description(registration),
        args_schema=registration.input_model,
    )


class GatewayVisibilityMiddleware(AgentMiddleware[AgentState, AgentInvocationContext]):
    """Resolve and bind the exact Gateway Tool Set before every model call."""

    def __init__(self, tools: Sequence[BaseTool]) -> None:
        self.tools = tuple(tools)
        self._tools_by_name = {tool.name: tool for tool in tools}

    def wrap_model_call(
        self,
        request: ModelRequest[AgentInvocationContext],
        handler: Callable[[ModelRequest[AgentInvocationContext]], ModelResponse],
    ) -> ModelResponse:
        context = request.runtime.context
        snapshot = context["gateway"].available_tools(
            environment=context["environment"],
            request_id=context["request_id"],
        )
        context["tool_set"] = snapshot
        visible = cast(
            list[BaseTool | dict[str, Any]],
            [
                self._tools_by_name[str(entry.name)]
                for entry in snapshot.tools
                if str(entry.name) in self._tools_by_name
            ],
        )
        return handler(request.override(tools=visible))


class AgentRuntime:
    """Thread-scoped standard Agent loop with checkpointed conversation memory."""

    def __init__(
        self,
        *,
        model: BaseChatModel,
        gateway: AgentGateway,
        registrations: Sequence[ToolRegistration],
        checkpointer: BaseCheckpointSaver[str] | None = None,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self._gateway = gateway
        self._tools = tuple(_gateway_tool(registration) for registration in registrations)
        visibility = GatewayVisibilityMiddleware(self._tools)
        interrupt_on: dict[str, bool | InterruptOnConfig] = {
            str(registration.definition.name): {
                "allowed_decisions": ["approve", "reject"],
                "description": "Autopilot execution requires explicit human approval.",
            }
            for registration in registrations
            if registration.definition.risk_level is RiskLevel.L2
        }
        middleware: list[AgentMiddleware[AgentState, AgentInvocationContext]] = [visibility]
        if interrupt_on:
            middleware.append(
                cast(
                    AgentMiddleware[AgentState, AgentInvocationContext],
                    HumanInTheLoopMiddleware(interrupt_on=interrupt_on),
                )
            )
        saver = checkpointer if checkpointer is not None else InMemorySaver()
        agent_builder = cast(
            Callable[
                ...,
                CompiledStateGraph[
                    AgentState,
                    AgentInvocationContext,
                    AgentState,
                    AgentState,
                ],
            ],
            create_agent,
        )
        self._agent = agent_builder(
            model=model,
            tools=self._tools,
            system_prompt=system_prompt,
            middleware=middleware,
            context_schema=AgentInvocationContext,
            checkpointer=saver,
            name="bittune-autopilot",
        )

    @staticmethod
    def _config(experiment_id: ExperimentId) -> RunnableConfig:
        return {"configurable": {"thread_id": str(experiment_id)}}

    def _context(self, environment: GatewayEnvironment) -> AgentInvocationContext:
        return {
            "gateway": self._gateway,
            "environment": environment,
            "request_id": f"agent-turn-{uuid4().hex}",
        }

    def _result(
        self,
        *,
        experiment_id: ExperimentId,
        output: Mapping[str, object],
        context: AgentInvocationContext,
    ) -> AgentRunResult:
        state = self._agent.get_state(self._config(experiment_id)).values
        raw_messages = state.get("messages", [])
        messages = tuple(message for message in raw_messages if isinstance(message, BaseMessage))
        snapshot = context.get("tool_set")
        return AgentRunResult(
            messages=tuple(_message_view(message) for message in messages),
            tool_calls=_tool_calls(messages),
            interrupted=_interrupt_value(output) is not None,
            interrupt_payload=_interrupt_value(output),
            tool_set_id=str(snapshot.tool_set_id) if snapshot is not None else None,
            tool_set_version=str(snapshot.tool_set_version) if snapshot is not None else None,
        )

    def send(
        self,
        *,
        experiment_id: ExperimentId,
        message: str,
        environment: GatewayEnvironment,
    ) -> AgentRunResult:
        """Append one user message and run the official model-tool loop for this turn."""
        if environment.experiment_id != experiment_id:
            raise ValueError(AGENT_ENVIRONMENT_MISMATCH)
        context = self._context(environment)
        input_state = cast(
            AgentState,
            {"messages": [HumanMessage(content=redact_conversation_text(message))]},
        )
        try:
            output = self._agent.invoke(
                input_state,
                config=self._config(experiment_id),
                context=context,
            )
        except (APIError, APITimeoutError) as error:
            raise AgentRuntimeError("MODEL_PROVIDER_UNAVAILABLE") from error
        return self._result(
            experiment_id=experiment_id,
            output=cast(Mapping[str, object], output),
            context=context,
        )

    def resume(
        self,
        *,
        experiment_id: ExperimentId,
        approved: bool,
        environment: GatewayEnvironment,
        message: str | None = None,
    ) -> AgentRunResult:
        """Resume the official HITL Interrupt with an approve or reject decision."""
        if environment.experiment_id != experiment_id:
            raise ValueError(AGENT_ENVIRONMENT_MISMATCH)
        context = self._context(environment)
        decision: dict[str, JsonValue] = {
            "type": "approve" if approved else "reject",
        }
        if message is not None:
            decision["message"] = redact_conversation_text(message)
        try:
            output = self._agent.invoke(
                Command(resume={"decisions": [decision]}),
                config=self._config(experiment_id),
                context=context,
            )
        except (APIError, APITimeoutError, ValueError) as error:
            raise AgentRuntimeError("AGENT_INTERRUPT_RESUME_FAILED") from error
        return self._result(
            experiment_id=experiment_id,
            output=cast(Mapping[str, object], output),
            context=context,
        )

    def state(self, *, experiment_id: ExperimentId) -> tuple[AgentMessageView, ...]:
        """Return the bounded conversation projection for an existing thread."""
        state = self._agent.get_state(self._config(experiment_id)).values
        raw_messages = state.get("messages", [])
        return tuple(
            _message_view(message) for message in raw_messages if isinstance(message, BaseMessage)
        )

    @staticmethod
    def _stream_interrupt_payload(value: object) -> dict[str, JsonValue] | None:
        if isinstance(value, (list, tuple)) and value:
            return _interrupt_value({"__interrupt__": value})
        return None

    def _stream_run(  # noqa: PLR0912
        self,
        *,
        experiment_id: ExperimentId,
        command: AgentState | Command[AgentState],
        context: AgentInvocationContext,
    ) -> Iterator[AgentStreamEvent]:
        """Adapt the official LangChain v2 stream into bounded presentation events."""
        final_output: dict[str, object] = {}
        try:
            stream = self._agent.stream(
                command,
                config=self._config(experiment_id),
                context=context,
                stream_mode=["messages", "updates"],
                version="v2",
            )
            for chunk in stream:
                chunk_type = chunk.get("type")
                data = chunk.get("data")
                if (
                    chunk_type == "messages"
                    and isinstance(data, tuple)
                    and len(data) == MESSAGE_STREAM_PART_SIZE
                ):
                    message = data[0]
                    if isinstance(message, BaseMessage) and message.type == "ai" and message.text:
                        yield AgentStreamEvent(
                            event_type="assistant.delta",
                            payload={"delta": redact_conversation_text(message.text)},
                        )
                    continue
                if chunk_type != "updates" or not isinstance(data, Mapping):
                    continue
                final_output.update(data)
                for source, update in data.items():
                    if source == "__interrupt__":
                        interrupt_payload = self._stream_interrupt_payload(update)
                        if interrupt_payload is not None:
                            yield AgentStreamEvent(
                                event_type="agent.interrupt",
                                payload=interrupt_payload,
                            )
                        continue
                    if not isinstance(update, Mapping):
                        continue
                    messages = update.get("messages")
                    if not isinstance(messages, (list, tuple)) or not messages:
                        continue
                    latest = messages[-1]
                    if isinstance(latest, AIMessage) and latest.tool_calls:
                        for call in latest.tool_calls:
                            view = AgentToolCallView(
                                tool_name=ToolName(root=call["name"]),
                                arguments=call["args"],
                                tool_call_id=call["id"],
                            )
                            yield AgentStreamEvent(
                                event_type="tool.call",
                                payload=cast(dict[str, JsonValue], view.model_dump(mode="json")),
                            )
                    elif isinstance(latest, ToolMessage):
                        yield AgentStreamEvent(
                            event_type="tool.result",
                            payload={
                                "tool_name": latest.name or "tool",
                                "tool_call_id": latest.tool_call_id,
                                "content": redact_conversation_text(latest.text),
                            },
                        )
            result = self._result(
                experiment_id=experiment_id,
                output=final_output,
                context=context,
            )
            yield AgentStreamEvent(
                event_type="run.completed",
                payload={
                    "interrupted": result.interrupted,
                    "assistant_message": result.assistant_message or "",
                },
                result=result,
            )
        except (APIError, APITimeoutError):
            yield AgentStreamEvent(
                event_type="run.error",
                payload={"code": "MODEL_PROVIDER_UNAVAILABLE"},
            )
            return
        except ValueError:
            yield AgentStreamEvent(
                event_type="run.error",
                payload={"code": "AGENT_STREAM_FAILED"},
            )
            return

    def stream_send(
        self,
        *,
        experiment_id: ExperimentId,
        message: str,
        environment: GatewayEnvironment,
    ) -> Iterator[AgentStreamEvent]:
        """Stream one user turn using the official LangChain v2 format."""
        if environment.experiment_id != experiment_id:
            raise ValueError(AGENT_ENVIRONMENT_MISMATCH)
        context = self._context(environment)
        input_state = cast(
            AgentState,
            {"messages": [HumanMessage(content=redact_conversation_text(message))]},
        )
        yield from self._stream_run(
            experiment_id=experiment_id,
            command=input_state,
            context=context,
        )

    def stream_resume(
        self,
        *,
        experiment_id: ExperimentId,
        approved: bool,
        environment: GatewayEnvironment,
        message: str | None = None,
    ) -> Iterator[AgentStreamEvent]:
        """Stream an approved or rejected official Agent Interrupt resume."""
        if environment.experiment_id != experiment_id:
            raise ValueError(AGENT_ENVIRONMENT_MISMATCH)
        context = self._context(environment)
        decision: dict[str, JsonValue] = {"type": "approve" if approved else "reject"}
        if message is not None:
            decision["message"] = redact_conversation_text(message)
        yield from self._stream_run(
            experiment_id=experiment_id,
            command=cast(Command[AgentState], Command(resume={"decisions": [decision]})),
            context=context,
        )


__all__ = [
    "AgentGateway",
    "AgentMessageView",
    "AgentRunResult",
    "AgentRuntime",
    "AgentRuntimeError",
    "AgentSessionPort",
    "AgentStreamEvent",
    "AgentToolCallView",
    "GatewayVisibilityMiddleware",
    "redact_conversation_text",
]
