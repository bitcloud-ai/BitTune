"""Small, persisted LangGraph state for one Autopilot Experiment."""

from __future__ import annotations

from typing import Literal, TypedDict

from pydantic import JsonValue, model_validator

from autopilot.domain.base import LongText, NonEmptyStr, StrictModel
from autopilot.domain.enums import ExperimentPhase, ExperimentStatus
from autopilot.domain.identifiers import ExperimentId

GraphSchemaVersion = Literal["autopilot-state/v1"]


class AutopilotState(TypedDict, total=False):
    """LangGraph state; only structured facts and stable references are allowed."""

    schema_version: GraphSchemaVersion
    thread_id: str
    experiment_id: str
    status: str
    phase: str
    user_message: str
    requirements: dict[str, JsonValue]
    test_strategy: dict[str, JsonValue]
    baseline_completed: bool
    hardware_passport_ref: str
    model_profile_ref: str
    workload_spec_ref: str
    slo_spec_ref: str
    candidate_refs: list[str]
    active_candidate_id: str
    active_deployment_id: str
    active_job_id: str
    active_study_id: str
    benchmark_summary_refs: list[str]
    trial_refs: list[str]
    champion_ref: str
    artifact_refs: list[str]
    approval_request: dict[str, JsonValue]
    approval_decision: str
    retry_count: int
    last_error: dict[str, JsonValue]
    warnings: list[dict[str, JsonValue]]


_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "password",
        "secret",
        "secret_ref",
        "token",
        "access_token",
        "refresh_token",
        "raw_log",
        "full_log",
        "model_file",
        "model_weights",
    }
)
SENSITIVE_STATE_FIELD = "Graph State cannot persist sensitive field"


def _reject_sensitive_keys(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in _SENSITIVE_KEYS:
                message = f"{SENSITIVE_STATE_FIELD}: {key}"
                raise ValueError(message)
            _reject_sensitive_keys(item)
    elif isinstance(value, list):
        for item in value:
            _reject_sensitive_keys(item)


class GraphStateSnapshot(StrictModel):
    """Validated projection used at API and checkpoint boundaries."""

    schema_version: GraphSchemaVersion = "autopilot-state/v1"
    thread_id: NonEmptyStr
    experiment_id: ExperimentId
    status: ExperimentStatus
    phase: ExperimentPhase
    user_message: LongText | None = None
    requirements: dict[str, JsonValue] | None = None
    test_strategy: dict[str, JsonValue] | None = None
    baseline_completed: bool = False
    hardware_passport_ref: NonEmptyStr | None = None
    model_profile_ref: NonEmptyStr | None = None
    workload_spec_ref: NonEmptyStr | None = None
    slo_spec_ref: NonEmptyStr | None = None
    candidate_refs: tuple[NonEmptyStr, ...] = ()
    active_candidate_id: NonEmptyStr | None = None
    active_deployment_id: NonEmptyStr | None = None
    active_job_id: NonEmptyStr | None = None
    active_study_id: NonEmptyStr | None = None
    benchmark_summary_refs: tuple[NonEmptyStr, ...] = ()
    trial_refs: tuple[NonEmptyStr, ...] = ()
    champion_ref: NonEmptyStr | None = None
    artifact_refs: tuple[NonEmptyStr, ...] = ()
    approval_request: dict[str, JsonValue] | None = None
    approval_decision: Literal["approved", "rejected"] | None = None
    retry_count: int = 0
    last_error: dict[str, JsonValue] | None = None
    warnings: tuple[dict[str, JsonValue], ...] = ()

    @model_validator(mode="before")
    @classmethod
    def reject_sensitive_state(cls, value: object) -> object:
        _reject_sensitive_keys(value)
        return value


def new_state(
    *, experiment_id: ExperimentId, thread_id: str, message: str | None
) -> AutopilotState:
    """Create the only valid initial state for a new Experiment."""
    state: AutopilotState = {
        "schema_version": "autopilot-state/v1",
        "thread_id": thread_id,
        "experiment_id": str(experiment_id),
        "status": ExperimentStatus.ACTIVE.value,
        "phase": ExperimentPhase.REQUIREMENTS.value,
        "candidate_refs": [],
        "benchmark_summary_refs": [],
        "trial_refs": [],
        "artifact_refs": [],
        "warnings": [],
        "retry_count": 0,
        "baseline_completed": False,
    }
    if message is not None:
        state["user_message"] = message
    GraphStateSnapshot.model_validate(state)
    return state


def validate_state(state: AutopilotState) -> AutopilotState:
    """Validate a state before it is handed to LangGraph or persistence."""
    GraphStateSnapshot.model_validate(state)
    return state


__all__ = ["AutopilotState", "GraphStateSnapshot", "new_state", "validate_state"]
