from datetime import UTC, datetime

from pydantic import BaseModel

from autopilot.capabilities.environment.tools import CreateEnvironmentPlanInput
from autopilot.domain.enums import (
    ExperimentPhase,
    JobKind,
    JobStatus,
    PlanKind,
    PlanStatus,
    RiskLevel,
    UserRole,
)
from autopilot.domain.identifiers import (
    ExperimentId,
    JobId,
    PlanHash,
    PlanId,
    Sha256Digest,
    ToolSetId,
    UserId,
)
from autopilot.domain.identities import HumanSubject
from autopilot.domain.jobs import JobRecord
from autopilot.domain.plans import PlanExecutionRequest
from autopilot.domain.requirements import RequirementSpec
from autopilot.gateway.models import AuthorizedReadOnlyCall, JobAuthorizationDraft
from autopilot.gateway.mvp_tools import (
    CreateExperimentPlanInput,
    DomainPlanResult,
    ExperimentPlanResult,
    JobQuery,
    JobQueryResult,
    JobSubmissionResult,
    MvpToolDispatcher,
    mvp_tool_registrations,
    provider_statuses,
)
from autopilot.gateway.registry import ToolRegistration


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


class RecordingDomainPlans:
    def __init__(self) -> None:
        self.kind: PlanKind | None = None
        self.risk_level: RiskLevel | None = None
        self.specification: BaseModel | None = None

    def create(
        self,
        *,
        kind: PlanKind,
        risk_level: RiskLevel,
        specification: BaseModel,
        authorization: AuthorizedReadOnlyCall,
    ) -> DomainPlanResult:
        self.kind = kind
        self.risk_level = risk_level
        self.specification = specification
        return DomainPlanResult(
            experiment_id=authorization.experiment_id,
            plan_id=PlanId(root="plan_" + "3" * 32),
            kind=kind,
            status=PlanStatus.APPROVED,
            risk_level=risk_level,
            plan_hash=PlanHash(root="sha256:" + "4" * 64),
            execution_schema_version="environment-execution-specification/v1",
            requires_approval=False,
        )


class RecordingJobs:
    def __init__(self) -> None:
        self.registration: ToolRegistration | None = None

    def enqueue(
        self,
        registration: ToolRegistration,
        authorization: JobAuthorizationDraft,
    ) -> JobSubmissionResult:
        self.registration = registration
        return JobSubmissionResult(
            experiment_id=authorization.experiment_id,
            job_id=JobId(root="job_" + "5" * 32),
            plan_id=authorization.plan_id,
            status=JobStatus.QUEUED,
            created=True,
        )

    def get(
        self,
        registration: ToolRegistration,
        query: JobQuery,
        authorization: AuthorizedReadOnlyCall,
    ) -> JobQueryResult:
        self.registration = registration
        return JobQueryResult(
            job=JobRecord(
                job_id=query.job_id,
                experiment_id=authorization.experiment_id,
                plan_id=PlanId.new(),
                kind=JobKind.ENVIRONMENT,
                status=JobStatus.QUEUED,
                submitted_at=authorization.authorized_at,
            )
        )

    def replay(
        self,
        registration: ToolRegistration,
        job_id: JobId,
        authorization: JobAuthorizationDraft,
    ) -> JobSubmissionResult:
        self.registration = registration
        return JobSubmissionResult(
            experiment_id=authorization.experiment_id,
            job_id=job_id,
            plan_id=authorization.plan_id,
            status=JobStatus.QUEUED,
            created=False,
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


def test_environment_plan_is_persisted_as_an_l1_domain_plan() -> None:
    registration = next(
        item
        for item in mvp_tool_registrations()
        if str(item.definition.name) == "create_environment_plan"
    )
    writer = RecordingDomainPlans()
    dispatcher = MvpToolDispatcher(provider_statuses=provider_statuses(), plans=writer)
    authorization = AuthorizedReadOnlyCall(
        experiment_id=ExperimentId.new(),
        subject=HumanSubject(user_id=UserId.new(), role=UserRole.OPERATOR),
        action=registration.definition.name,
        tool_schema_version=registration.definition.input_schema_version,
        tool_set_id=ToolSetId.new(),
        tool_set_version=Sha256Digest(root="sha256:" + "8" * 64),
        policy_decision_id="decision-2",
        request_hash=Sha256Digest(root="sha256:" + "7" * 64),
        authorized_at=datetime(2026, 8, 6, tzinfo=UTC),
    )
    arguments = CreateEnvironmentPlanInput.model_validate(
        {
            "specification": {
                "provider_version": "13.610.43",
                "adapter_version": "environment-adapter-v1",
                "provider_profile_version": "rtx5090-v1",
                "scope": "mvp_full",
                "include_runtime_probe": True,
            }
        }
    )

    result = dispatcher.invoke_read_only(registration, arguments, authorization)

    assert isinstance(result, DomainPlanResult)
    assert writer.kind is PlanKind.ENVIRONMENT
    assert writer.risk_level is RiskLevel.L1
    assert writer.specification == arguments.specification


def test_start_tool_enqueues_the_authorized_plan() -> None:
    registration = next(
        item
        for item in mvp_tool_registrations()
        if str(item.definition.name) == "start_environment_inspection"
    )
    writer = RecordingJobs()
    dispatcher = MvpToolDispatcher(provider_statuses=provider_statuses(), jobs=writer)
    experiment_id = ExperimentId.new()
    plan_id = PlanId.new()
    plan_hash = PlanHash(root="sha256:" + "6" * 64)
    authorization = JobAuthorizationDraft(
        experiment_id=experiment_id,
        subject=HumanSubject(user_id=UserId.new(), role=UserRole.OPERATOR),
        action=registration.definition.name,
        risk_level=RiskLevel.L1,
        plan_id=plan_id,
        plan_hash=plan_hash,
        tool_schema_version=registration.definition.input_schema_version,
        tool_set_id=ToolSetId.new(),
        tool_set_version=Sha256Digest(root="sha256:" + "8" * 64),
        policy_decision_id="decision-3",
        request_hash=Sha256Digest(root="sha256:" + "7" * 64),
        idempotency_key=Sha256Digest(root="sha256:" + "9" * 64),
        authorized_at=datetime(2026, 8, 6, tzinfo=UTC),
    )
    arguments = PlanExecutionRequest(plan_id=plan_id, expected_plan_hash=plan_hash)

    result = dispatcher.enqueue_job(registration, arguments, authorization)

    assert isinstance(result, JobSubmissionResult)
    assert result.plan_id == plan_id
    assert result.status is JobStatus.QUEUED
    assert writer.registration is registration


def test_job_status_tool_reads_the_persisted_job_projection() -> None:
    registration = next(
        item
        for item in mvp_tool_registrations()
        if str(item.definition.name) == "get_environment_status"
    )
    writer = RecordingJobs()
    dispatcher = MvpToolDispatcher(provider_statuses=provider_statuses(), jobs=writer)
    experiment_id = ExperimentId.new()
    authorization = AuthorizedReadOnlyCall(
        experiment_id=experiment_id,
        subject=HumanSubject(user_id=UserId.new(), role=UserRole.OPERATOR),
        action=registration.definition.name,
        tool_schema_version=registration.definition.input_schema_version,
        tool_set_id=ToolSetId.new(),
        tool_set_version=Sha256Digest(root="sha256:" + "8" * 64),
        policy_decision_id="decision-4",
        request_hash=Sha256Digest(root="sha256:" + "7" * 64),
        authorized_at=datetime(2026, 8, 6, tzinfo=UTC),
    )
    job_id = JobId.new()

    result = dispatcher.invoke_read_only(registration, JobQuery(job_id=job_id), authorization)

    assert isinstance(result, JobQueryResult)
    assert result.job.job_id == job_id
    assert result.job.experiment_id == experiment_id
    assert writer.registration is registration
