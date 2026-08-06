import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from autopilot.domain.enums import ExperimentPhase
from autopilot.domain.identifiers import ExperimentId
from autopilot.graph.reconciliation import NoopReconciler
from autopilot.graph.state import GraphStateSnapshot, new_state
from autopilot.graph.workflow import GraphDependencies, build_runtime
from tests.unit.graph.fakes import FakeGraphOperations, FakeModelProvider


def test_main_graph_interrupts_twice_and_recovers_from_same_checkpointer() -> None:
    operations = FakeGraphOperations()
    dependencies = GraphDependencies(FakeModelProvider(), operations, NoopReconciler())
    saver = InMemorySaver()
    experiment_id = ExperimentId.new()
    first_runtime = build_runtime(dependencies, checkpointer=saver)

    first = first_runtime.start(
        experiment_id=experiment_id,
        state=new_state(
            experiment_id=experiment_id,
            thread_id=str(experiment_id),
            message="optimize Qwen for throughput",
        ),
    )
    assert first.interrupted
    assert first.state.phase is ExperimentPhase.APPROVAL
    assert first.interrupt_payload is not None
    assert first.interrupt_payload["action"] == "start_deployment"

    restarted_runtime = build_runtime(dependencies, checkpointer=saver)
    second = restarted_runtime.resume(
        experiment_id=experiment_id,
        answer={"decision": "approved"},
    )
    assert second.interrupted
    assert second.interrupt_payload is not None
    assert second.interrupt_payload["action"] == "approve_champion"

    completed = restarted_runtime.resume(
        experiment_id=experiment_id,
        answer={"decision": "approved"},
    )
    assert not completed.interrupted
    assert completed.state.phase is ExperimentPhase.COMPLETED
    assert completed.state.champion_ref is not None
    assert operations.calls == [
        "environment",
        "capacity",
        "deployment",
        "baseline",
        "strategy",
        "optimization",
        "verification",
        "evidence",
    ]


def test_graph_state_rejects_nested_secret_material() -> None:
    experiment_id = ExperimentId.new()
    state = new_state(
        experiment_id=experiment_id,
        thread_id=str(experiment_id),
        message="safe request",
    )
    state["requirements"] = {"api_key": "must-not-persist"}

    with pytest.raises(ValidationError, match="sensitive field"):
        GraphStateSnapshot.model_validate(state)
