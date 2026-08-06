"""The single deterministic Autopilot Graph used by the MVP."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from pydantic import Field, JsonValue

from autopilot.domain.base import NonEmptyStr, StrictModel
from autopilot.domain.enums import ErrorCategory, ExperimentPhase, ExperimentStatus, SuggestedAction
from autopilot.domain.errors import DomainError, ErrorEnvelope
from autopilot.domain.identifiers import ExperimentId
from autopilot.domain.requirements import RequirementSpec
from autopilot.graph.model_provider import (
    BenchmarkIntent,
    ModelProvider,
    ModelProviderError,
    ReportDraft,
)
from autopilot.graph.reconciliation import ReconciliationPort
from autopilot.graph.state import AutopilotState, GraphStateSnapshot, validate_state


class GraphExecutionError(RuntimeError):
    """Typed deterministic-node failure with no provider stack trace in State."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "GRAPH_EXECUTION_FAILED",
        category: ErrorCategory = ErrorCategory.INFRASTRUCTURE_ERROR,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.category = category
        self.retryable = retryable


class GraphOperationResult(StrictModel):
    """Structured references returned by existing capability services."""

    schema_version: str = "graph-operation-result/v1"
    hardware_passport_ref: NonEmptyStr | None = None
    model_profile_ref: NonEmptyStr | None = None
    workload_spec_ref: NonEmptyStr | None = None
    slo_spec_ref: NonEmptyStr | None = None
    candidate_refs: tuple[NonEmptyStr, ...] = Field(default=(), max_length=16)
    active_candidate_id: NonEmptyStr | None = None
    active_deployment_id: NonEmptyStr | None = None
    active_job_id: NonEmptyStr | None = None
    active_study_id: NonEmptyStr | None = None
    benchmark_summary_refs: tuple[NonEmptyStr, ...] = Field(default=(), max_length=32)
    trial_refs: tuple[NonEmptyStr, ...] = Field(default=(), max_length=256)
    champion_ref: NonEmptyStr | None = None
    artifact_refs: tuple[NonEmptyStr, ...] = Field(default=(), max_length=256)
    warnings: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=16)


class GraphOperations(Protocol):
    """Application boundary for the already implemented capability packages."""

    def inspect_environment(self, state: AutopilotState) -> GraphOperationResult: ...

    def estimate_capacity(self, state: AutopilotState) -> GraphOperationResult: ...

    def deploy_and_smoke_test(self, state: AutopilotState) -> GraphOperationResult: ...

    def benchmark_baseline(self, state: AutopilotState) -> GraphOperationResult: ...

    def benchmark_strategy(
        self, state: AutopilotState, intent: BenchmarkIntent
    ) -> GraphOperationResult: ...

    def optimize(self, state: AutopilotState) -> GraphOperationResult: ...

    def verify_top_candidates(self, state: AutopilotState) -> GraphOperationResult: ...

    def archive_evidence(
        self, state: AutopilotState, report: ReportDraft
    ) -> GraphOperationResult: ...


class UnavailableGraphOperations:
    """Fail-closed production default until the fixed Provider profile is configured."""

    def _unavailable(self, name: str) -> GraphOperationResult:
        raise GraphExecutionError(
            f"Provider operation {name} is not configured",
            code="PROVIDER_PROFILE_UNAVAILABLE",
            category=ErrorCategory.INFRASTRUCTURE_ERROR,
        )

    def inspect_environment(self, state: AutopilotState) -> GraphOperationResult:
        return self._unavailable("inspect_environment")

    def estimate_capacity(self, state: AutopilotState) -> GraphOperationResult:
        return self._unavailable("estimate_capacity")

    def deploy_and_smoke_test(self, state: AutopilotState) -> GraphOperationResult:
        return self._unavailable("deploy_and_smoke_test")

    def benchmark_baseline(self, state: AutopilotState) -> GraphOperationResult:
        return self._unavailable("benchmark_baseline")

    def benchmark_strategy(
        self, state: AutopilotState, intent: BenchmarkIntent
    ) -> GraphOperationResult:
        return self._unavailable("benchmark_strategy")

    def optimize(self, state: AutopilotState) -> GraphOperationResult:
        return self._unavailable("optimize")

    def verify_top_candidates(self, state: AutopilotState) -> GraphOperationResult:
        return self._unavailable("verify_top_candidates")

    def archive_evidence(self, state: AutopilotState, report: ReportDraft) -> GraphOperationResult:
        return self._unavailable("archive_evidence")


class GraphReconciliationError(GraphExecutionError):
    """Raised when external state cannot be safely reconciled."""


@dataclass(frozen=True, slots=True)
class GraphDependencies:
    model_provider: ModelProvider
    operations: GraphOperations
    reconciler: ReconciliationPort


def _set_phase(state: AutopilotState, phase: ExperimentPhase) -> AutopilotState:
    payload = dict(state)
    payload["phase"] = phase.value
    payload["status"] = (
        ExperimentStatus.WAITING_APPROVAL.value
        if phase is ExperimentPhase.APPROVAL
        else ExperimentStatus.ACTIVE.value
    )
    if phase is ExperimentPhase.COMPLETED:
        payload["status"] = ExperimentStatus.COMPLETED.value
    elif phase is ExperimentPhase.FAILED:
        payload["status"] = ExperimentStatus.FAILED.value
    elif phase is ExperimentPhase.CANCELLED:
        payload["status"] = ExperimentStatus.CANCELLED.value
    return validate_state(cast(AutopilotState, payload))


def _apply_result(state: AutopilotState, result: GraphOperationResult) -> AutopilotState:
    payload = dict(state)
    for field in (
        "hardware_passport_ref",
        "model_profile_ref",
        "workload_spec_ref",
        "slo_spec_ref",
        "active_candidate_id",
        "active_deployment_id",
        "active_job_id",
        "active_study_id",
        "champion_ref",
    ):
        value = getattr(result, field)
        if value is not None:
            payload[field] = str(value)
    for field in ("candidate_refs", "benchmark_summary_refs", "trial_refs", "artifact_refs"):
        existing = list(cast(list[str], payload.get(field, [])))
        for value in getattr(result, field):
            if str(value) not in existing:
                existing.append(str(value))
        payload[field] = existing
    warnings = list(cast(list[dict[str, JsonValue]], payload.get("warnings", [])))
    warnings.extend(result.warnings)
    payload["warnings"] = warnings
    return validate_state(cast(AutopilotState, payload))


def _error_envelope(error: GraphExecutionError | ModelProviderError) -> ErrorEnvelope:
    if isinstance(error, ModelProviderError):
        code = "MODEL_PROVIDER_UNAVAILABLE"
        category = ErrorCategory.INFRASTRUCTURE_ERROR
        message = "远程 ModelProvider 暂不可用"
        retryable = True
    else:
        code = error.code
        category = error.category
        message = str(error)
        retryable = error.retryable
    return ErrorEnvelope(
        error=DomainError(
            code=code,
            category=category,
            message=message,
            retryable=retryable,
            suggested_actions=(SuggestedAction.CONTACT_OPERATOR,),
        )
    )


def _failed(
    state: AutopilotState, error: GraphExecutionError | ModelProviderError
) -> AutopilotState:
    payload = dict(state)
    payload["last_error"] = cast(
        dict[str, JsonValue], _error_envelope(error).model_dump(mode="json")
    )
    current_retry_count = payload.get("retry_count", 0)
    payload["retry_count"] = current_retry_count + 1 if isinstance(current_retry_count, int) else 1
    return _set_phase(cast(AutopilotState, payload), ExperimentPhase.FAILED)


def _run_operation(
    state: AutopilotState,
    operation: object,
) -> AutopilotState:
    try:
        result = cast(GraphOperationResult, operation)
        return _apply_result(state, result)
    except GraphExecutionError as error:
        return _failed(state, error)
    except ModelProviderError as error:
        return _failed(state, error)
    except Exception:
        return _failed(
            state,
            GraphExecutionError(
                "deterministic graph operation failed",
                code="GRAPH_OPERATION_FAILED",
                category=ErrorCategory.INFRASTRUCTURE_ERROR,
            ),
        )


def _reconcile(dependencies: GraphDependencies, state: AutopilotState) -> AutopilotState:
    try:
        result = dependencies.reconciler.reconcile(state)
        payload = dict(state)
        if result.active_job_ref is not None:
            payload["active_job_id"] = str(result.active_job_ref)
        if result.active_deployment_ref is not None:
            payload["active_deployment_id"] = str(result.active_deployment_ref)
        payload["warnings"] = list(cast(list[dict[str, JsonValue]], payload.get("warnings", [])))
        cast(list[dict[str, JsonValue]], payload["warnings"]).extend(result.warnings)
        if result.requires_failure:
            raise GraphReconciliationError(
                result.failure_code or "external state reconciliation failed",
                code="RECONCILIATION_FAILED",
            )
        return validate_state(cast(AutopilotState, payload))
    except GraphExecutionError as error:
        return _failed(state, error)
    except Exception:
        return _failed(
            state,
            GraphReconciliationError(
                "external state reconciliation failed", code="RECONCILIATION_FAILED"
            ),
        )


def _approval_payload(state: AutopilotState, *, action: str, summary: str) -> dict[str, JsonValue]:
    return {
        "schema_version": "graph-approval-interrupt/v1",
        "experiment_id": state["experiment_id"],
        "action": action,
        "risk_level": "L2",
        "summary": summary,
        "plan_id": str(state.get("active_job_id", "pending-plan")),
    }


def _decision(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        decision = value.get("decision")
        return decision if isinstance(decision, str) else ""
    return ""


def build_graph(
    dependencies: GraphDependencies,
    *,
    checkpointer: BaseCheckpointSaver[str] | None = None,
) -> CompiledStateGraph[AutopilotState, None, AutopilotState, AutopilotState]:
    """Build one graph; all side-effecting work remains in injected deterministic services."""
    saver = checkpointer if checkpointer is not None else InMemorySaver()
    builder = StateGraph(AutopilotState)

    def reconcile(state: AutopilotState) -> AutopilotState:
        return _reconcile(dependencies, state)

    def parse_requirements(state: AutopilotState) -> AutopilotState:
        if state.get("requirements") is not None:
            return _set_phase(state, ExperimentPhase.ENVIRONMENT)
        message = state.get("user_message")
        if not message:
            answer = interrupt({"kind": "requirements_input", "schema_version": "requirements/v1"})
            if not isinstance(answer, Mapping) or not isinstance(answer.get("message"), str):
                return _failed(
                    state,
                    GraphExecutionError(
                        "requirements input is missing",
                        code="REQUIREMENTS_INPUT_MISSING",
                        category=ErrorCategory.VALIDATION_ERROR,
                    ),
                )
            message = answer["message"]
        try:
            requirements = dependencies.model_provider.parse_requirements(message)
        except ModelProviderError as error:
            return _failed(state, error)
        except Exception:
            return _failed(
                state,
                GraphExecutionError(
                    "ModelProvider returned invalid requirements",
                    code="REQUIREMENTS_SCHEMA_INVALID",
                    category=ErrorCategory.VALIDATION_ERROR,
                ),
            )
        payload = dict(state)
        payload["requirements"] = cast(dict[str, JsonValue], requirements.model_dump(mode="json"))
        payload.pop("user_message", None)
        return _set_phase(cast(AutopilotState, payload), ExperimentPhase.ENVIRONMENT)

    def inspect_environment(state: AutopilotState) -> AutopilotState:
        try:
            result = dependencies.operations.inspect_environment(state)
            return _set_phase(_apply_result(state, result), ExperimentPhase.PLANNING)
        except (GraphExecutionError, ModelProviderError) as error:
            return _failed(state, error)

    def plan_capacity(state: AutopilotState) -> AutopilotState:
        try:
            result = dependencies.operations.estimate_capacity(state)
            payload = _apply_result(state, result)
            payload["approval_request"] = _approval_payload(
                payload,
                action="start_deployment",
                summary="将依据已校验的 Candidate 启动 vLLM 并执行 Smoke Test",
            )
            return _set_phase(payload, ExperimentPhase.APPROVAL)
        except (GraphExecutionError, ModelProviderError) as error:
            return _failed(state, error)

    def deployment_approval(state: AutopilotState) -> AutopilotState:
        answer = interrupt(state.get("approval_request", {"kind": "deployment_approval"}))
        if _decision(answer) != "approved":
            return _set_phase(
                cast(AutopilotState, {**state, "approval_decision": "rejected"}),
                ExperimentPhase.FAILED,
            )
        return _set_phase(
            cast(AutopilotState, {**state, "approval_decision": "approved"}),
            ExperimentPhase.DEPLOYMENT,
        )

    def deploy(state: AutopilotState) -> AutopilotState:
        try:
            result = dependencies.operations.deploy_and_smoke_test(state)
            return _set_phase(_apply_result(state, result), ExperimentPhase.BENCHMARK)
        except (GraphExecutionError, ModelProviderError) as error:
            return _failed(state, error)

    def benchmark(state: AutopilotState) -> AutopilotState:
        try:
            result = dependencies.operations.benchmark_baseline(state)
            payload = _apply_result(state, result)
            payload["baseline_completed"] = True
            return _set_phase(payload, ExperimentPhase.BENCHMARK)
        except (GraphExecutionError, ModelProviderError) as error:
            return _failed(state, error)

    def propose_strategy(state: AutopilotState) -> AutopilotState:
        try:
            requirements = RequirementSpec.model_validate(state.get("requirements", {}))
            intent = dependencies.model_provider.propose_test_strategy(requirements)
            result = dependencies.operations.benchmark_strategy(state, intent)
            payload = _apply_result(state, result)
            payload["test_strategy"] = cast(dict[str, JsonValue], intent.model_dump(mode="json"))
            return _set_phase(payload, ExperimentPhase.OPTIMIZATION)
        except ModelProviderError as error:
            return _failed(state, error)
        except (GraphExecutionError, ValueError):
            return _failed(
                state,
                GraphExecutionError(
                    "test strategy could not be validated",
                    code="TEST_STRATEGY_INVALID",
                    category=ErrorCategory.VALIDATION_ERROR,
                ),
            )

    def optimize(state: AutopilotState) -> AutopilotState:
        try:
            result = dependencies.operations.optimize(state)
            return _set_phase(_apply_result(state, result), ExperimentPhase.VERIFICATION)
        except (GraphExecutionError, ModelProviderError) as error:
            return _failed(state, error)

    def verify(state: AutopilotState) -> AutopilotState:
        try:
            result = dependencies.operations.verify_top_candidates(state)
            payload = _apply_result(state, result)
            payload["approval_request"] = _approval_payload(
                payload,
                action="approve_champion",
                summary="将把通过硬约束和重复复测的 Candidate 标记为 Champion",
            )
            return _set_phase(payload, ExperimentPhase.APPROVAL)
        except (GraphExecutionError, ModelProviderError) as error:
            return _failed(state, error)

    def champion_approval(state: AutopilotState) -> AutopilotState:
        answer = interrupt(state.get("approval_request", {"kind": "champion_approval"}))
        if _decision(answer) != "approved":
            return _set_phase(
                cast(AutopilotState, {**state, "approval_decision": "rejected"}),
                ExperimentPhase.FAILED,
            )
        return _set_phase(
            cast(AutopilotState, {**state, "approval_decision": "approved"}),
            ExperimentPhase.REPORT,
        )

    def report(state: AutopilotState) -> AutopilotState:
        try:
            draft = dependencies.model_provider.write_report(tuple(state.get("artifact_refs", [])))
            result = dependencies.operations.archive_evidence(state, draft)
            return _set_phase(_apply_result(state, result), ExperimentPhase.COMPLETED)
        except (GraphExecutionError, ModelProviderError) as error:
            return _failed(state, error)

    nodes = {
        "reconcile": reconcile,
        "parse_requirements": parse_requirements,
        "inspect_environment": inspect_environment,
        "plan_capacity": plan_capacity,
        "deployment_approval": deployment_approval,
        "deploy": deploy,
        "benchmark": benchmark,
        "propose_strategy": propose_strategy,
        "optimize": optimize,
        "verify": verify,
        "champion_approval": champion_approval,
        "report": report,
    }
    for name, node in nodes.items():
        builder.add_node(name, node)
    builder.add_edge(START, "reconcile")
    for name in nodes:
        if name != "reconcile":
            builder.add_edge(name, "reconcile")

    def route(state: AutopilotState) -> str:
        if state["phase"] in {ExperimentPhase.COMPLETED.value, ExperimentPhase.FAILED.value}:
            return END
        if state["phase"] == ExperimentPhase.REQUIREMENTS.value:
            return "parse_requirements"
        if state["phase"] == ExperimentPhase.ENVIRONMENT.value:
            return "inspect_environment"
        if state["phase"] == ExperimentPhase.PLANNING.value:
            return "plan_capacity"
        if state["phase"] == ExperimentPhase.APPROVAL.value:
            action = state.get("approval_request", {}).get("action")
            return "champion_approval" if action == "approve_champion" else "deployment_approval"
        if state["phase"] == ExperimentPhase.DEPLOYMENT.value:
            return "deploy"
        if state["phase"] == ExperimentPhase.BENCHMARK.value:
            if not state.get("baseline_completed", False):
                return "benchmark"
            return "propose_strategy"
        if state["phase"] == ExperimentPhase.OPTIMIZATION.value:
            return "optimize"
        if state["phase"] == ExperimentPhase.VERIFICATION.value:
            return "verify"
        if state["phase"] == ExperimentPhase.REPORT.value:
            return "report"
        return END

    builder.add_conditional_edges(
        "reconcile",
        route,
        {
            "parse_requirements": "parse_requirements",
            "inspect_environment": "inspect_environment",
            "plan_capacity": "plan_capacity",
            "deployment_approval": "deployment_approval",
            "deploy": "deploy",
            "benchmark": "benchmark",
            "propose_strategy": "propose_strategy",
            "optimize": "optimize",
            "verify": "verify",
            "champion_approval": "champion_approval",
            "report": "report",
            END: END,
        },
    )
    return builder.compile(checkpointer=saver)


@dataclass(frozen=True, slots=True)
class GraphRunResult:
    state: GraphStateSnapshot
    interrupted: bool
    interrupt_payload: dict[str, JsonValue] | None


class GraphRuntime:
    """Thread-scoped invoke/resume wrapper around the compiled main Graph."""

    def __init__(
        self,
        graph: CompiledStateGraph[AutopilotState, None, AutopilotState, AutopilotState],
    ) -> None:
        self._graph = graph

    def _config(self, experiment_id: ExperimentId) -> RunnableConfig:
        return {"configurable": {"thread_id": str(experiment_id)}}

    def _result(self, output: Mapping[str, object], experiment_id: ExperimentId) -> GraphRunResult:
        state = self._graph.get_state(self._config(experiment_id)).values
        snapshot = GraphStateSnapshot.model_validate(state)
        interrupts = output.get("__interrupt__")
        payload: dict[str, JsonValue] | None = None
        if isinstance(interrupts, (list, tuple)) and interrupts:
            value = getattr(interrupts[0], "value", None)
            if isinstance(value, dict):
                payload = cast(dict[str, JsonValue], value)
        return GraphRunResult(snapshot, payload is not None, payload)

    def start(self, *, experiment_id: ExperimentId, state: AutopilotState) -> GraphRunResult:
        output = self._graph.invoke(state, config=self._config(experiment_id))
        return self._result(cast(Mapping[str, object], output), experiment_id)

    def resume(self, *, experiment_id: ExperimentId, answer: object) -> GraphRunResult:
        output = self._graph.invoke(
            Command(resume=answer),
            config=self._config(experiment_id),
        )
        return self._result(cast(Mapping[str, object], output), experiment_id)

    def state(self, *, experiment_id: ExperimentId) -> GraphStateSnapshot:
        values = self._graph.get_state(self._config(experiment_id)).values
        return GraphStateSnapshot.model_validate(values)


def build_runtime(
    dependencies: GraphDependencies,
    *,
    checkpointer: BaseCheckpointSaver[str] | None = None,
) -> GraphRuntime:
    return GraphRuntime(build_graph(dependencies, checkpointer=checkpointer))


__all__ = [
    "GraphDependencies",
    "GraphExecutionError",
    "GraphOperationResult",
    "GraphOperations",
    "GraphRuntime",
    "UnavailableGraphOperations",
    "build_graph",
    "build_runtime",
]
