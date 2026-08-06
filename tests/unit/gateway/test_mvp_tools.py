from datetime import UTC, datetime

from autopilot.domain.enums import ExperimentPhase, UserRole
from autopilot.domain.identifiers import ExperimentId, Sha256Digest, ToolSetId, UserId
from autopilot.domain.identities import HumanSubject
from autopilot.domain.requirements import RequirementSpec
from autopilot.gateway.models import AuthorizedReadOnlyCall
from autopilot.gateway.mvp_tools import (
    CreateExperimentPlanInput,
    ExperimentPlanResult,
    MvpToolDispatcher,
    mvp_tool_registrations,
    provider_statuses,
)


class RecordingExperimentPlans:
    def __init__(self) -> None:
        self.requirements: RequirementSpec | None = None

    def create(
        self,
        requirements: RequirementSpec,
        authorization: AuthorizedReadOnlyCall,
    ) -> ExperimentPlanResult:
        assert requirements.created_by == authorization.subject.user_id
        self.requirements = requirements
        return ExperimentPlanResult(
            experiment_id=authorization.experiment_id,
            requirements_hash=Sha256Digest(root="sha256:" + "9" * 64),
        )


def _input() -> CreateExperimentPlanInput:
    return CreateExperimentPlanInput.model_validate(
        {
            "model_ref": {
                "type": "huggingface",
                "repository_id": "Qwen/Qwen3-8B",
                "revision": "a" * 40,
            },
            "priority": "throughput",
            "workload": {
                "dataset": {"type": "synthetic_fixed", "dataset_id": "medium-v1"},
                "tokenizer": {"repository_id": "Qwen/Qwen3-8B", "revision": "a" * 40},
                "prompt_tokens": 2048,
                "output_tokens": 512,
                "stream": True,
                "ignore_eos": False,
                "sampling": {"temperature": 0, "seed": 7},
            },
            "slo": {
                "constraints": [
                    {
                        "kind": "numeric",
                        "metric": "ttft_p95_ms",
                        "operator": "<=",
                        "value": 2000,
                    }
                ]
            },
            "budget": {
                "max_duration_seconds": 1800,
                "max_requests": 1000,
                "max_input_tokens": 2_048_000,
                "max_output_tokens": 512_000,
                "max_disk_growth_bytes": 1_000_000_000,
            },
            "allow_model_download": True,
            "allow_container_start": True,
        }
    )


def test_requirements_phase_registers_and_dispatches_create_experiment_plan() -> None:
    registration = next(
        item
        for item in mvp_tool_registrations()
        if str(item.definition.name) == "create_experiment_plan"
    )
    writer = RecordingExperimentPlans()
    dispatcher = MvpToolDispatcher(
        provider_statuses=provider_statuses(),
        experiment_plans=writer,
    )
    subject = HumanSubject(user_id=UserId.new(), role=UserRole.OPERATOR)
    authorization = AuthorizedReadOnlyCall(
        experiment_id=ExperimentId.new(),
        subject=subject,
        action=registration.definition.name,
        tool_schema_version=registration.definition.input_schema_version,
        tool_set_id=ToolSetId.new(),
        tool_set_version=Sha256Digest(root="sha256:" + "8" * 64),
        policy_decision_id="decision-1",
        request_hash=Sha256Digest(root="sha256:" + "7" * 64),
        authorized_at=datetime(2026, 8, 6, tzinfo=UTC),
    )

    result = dispatcher.invoke_read_only(registration, _input(), authorization)

    assert registration.definition.allowed_phases == (ExperimentPhase.REQUIREMENTS,)
    assert isinstance(result, ExperimentPlanResult)
    assert result.experiment_id == authorization.experiment_id
    assert writer.requirements is not None
    assert writer.requirements.created_by == subject.user_id
    assert writer.requirements.model_ref.repository_id == "Qwen/Qwen3-8B"
