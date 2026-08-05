from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from pydantic import BaseModel

from autopilot.domain.base import StrictModel
from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.enums import (
    ApprovalDecision,
    ExperimentPhase,
    PlanStatus,
    RiskLevel,
    UserRole,
)
from autopilot.domain.identifiers import (
    ApprovalId,
    ExperimentId,
    JobId,
    PlanHash,
    PlanId,
    Sha256Digest,
    ToolName,
    ToolSetId,
    UserId,
)
from autopilot.domain.identities import HumanSubject
from autopilot.domain.plans import PlanExecutionRequest
from autopilot.gateway.errors import (
    ApprovalAuthorizationError,
    GatewayErrorCode,
    IdempotencyAuthorizationError,
    PlanAuthorizationError,
    ToolGatewayError,
    WorkflowStateError,
)
from autopilot.gateway.models import (
    AuthorizedReadOnlyCall,
    GatewayEnvironment,
    JobAuthorizationDraft,
    JobIdempotencyClaim,
    PlanAuthorizationMaterial,
    ToolCallRequest,
    ToolDefinition,
    ToolExecutionMode,
    ToolSetEntry,
    ToolSetSnapshot,
    VisibilityContext,
    create_toolset_snapshot,
)
from autopilot.gateway.registry import ToolRegistration, ToolRegistry
from autopilot.gateway.service import GatewayDependencies, ToolGateway
from autopilot.jobs.models import AuditEvent, AuditResult
from autopilot.policy.models import (
    PolicyApproval,
    PolicyDecision,
    PolicyHumanSubject,
    PolicyInput,
    PolicyReasonCode,
    PolicyRequirements,
)
from autopilot.policy.opa import PolicyResponseError, PolicyUnavailableError

NOW = datetime(2026, 8, 6, 1, 2, 3, tzinfo=UTC)
PLAN_HASH = PlanHash(root=f"sha256:{'1' * 64}")
START_DEPLOYMENT = ToolName(root="start_deployment")


class EnqueueResult(StrictModel):
    schema_version: Literal["enqueue-result/v1"] = "enqueue-result/v1"
    job_id: JobId
    created: bool


class ReadResult(StrictModel):
    schema_version: Literal["read-result/v1"] = "read-result/v1"
    value: str


class FakePolicy:
    def __init__(
        self,
        decision: PolicyDecision | None = None,
        error: PolicyUnavailableError | PolicyResponseError | None = None,
    ) -> None:
        self.decision = decision or policy_decision()
        self.error = error
        self.calls: list[PolicyInput] = []

    def evaluate(self, policy_input: PolicyInput) -> PolicyDecision:
        self.calls.append(policy_input)
        if self.error is not None:
            raise self.error
        return self.decision


class FakeWorkflow:
    def __init__(self, phase: ExperimentPhase) -> None:
        self.phase = phase
        self.error: WorkflowStateError | None = None
        self.calls: list[ExperimentId] = []

    def current_phase(self, experiment_id: ExperimentId) -> ExperimentPhase:
        self.calls.append(experiment_id)
        if self.error is not None:
            raise self.error
        return self.phase


class FakeToolSets:
    def __init__(self, snapshot: ToolSetSnapshot) -> None:
        self.snapshots = {snapshot.tool_set_id: snapshot}

    def add(self, snapshot: ToolSetSnapshot) -> None:
        self.snapshots[snapshot.tool_set_id] = snapshot

    def get(self, tool_set_id: ToolSetId) -> ToolSetSnapshot | None:
        return self.snapshots.get(tool_set_id)


class FakePlans:
    def __init__(self, plan: PlanAuthorizationMaterial) -> None:
        self.plan = plan
        self.error: PlanAuthorizationError | None = None
        self.calls: list[tuple[ExperimentId, PlanId, PlanHash]] = []

    def get_for_execution(
        self,
        *,
        experiment_id: ExperimentId,
        plan_id: PlanId,
        expected_plan_hash: PlanHash,
    ) -> PlanAuthorizationMaterial:
        self.calls.append((experiment_id, plan_id, expected_plan_hash))
        if self.error is not None:
            raise self.error
        return self.plan


class FakeApprovals:
    def __init__(self, candidate: PolicyApproval | None) -> None:
        self.candidate = candidate
        self.candidate_error: ApprovalAuthorizationError | None = None
        self.validation_error: ApprovalAuthorizationError | None = None
        self.candidate_calls: list[tuple[ExperimentId, PlanId, PlanHash, ToolName]] = []
        self.validation_calls: list[
            tuple[ApprovalId, ExperimentId, PlanId, PlanHash, ToolName]
        ] = []

    def get_candidate(
        self,
        *,
        experiment_id: ExperimentId,
        plan_id: PlanId,
        plan_hash: PlanHash,
        action: ToolName,
    ) -> PolicyApproval | None:
        self.candidate_calls.append((experiment_id, plan_id, plan_hash, action))
        if self.candidate_error is not None:
            raise self.candidate_error
        return self.candidate

    def require_valid_for_execution(
        self,
        *,
        approval_id: ApprovalId,
        experiment_id: ExperimentId,
        plan_id: PlanId,
        plan_hash: PlanHash,
        action: ToolName,
    ) -> ApprovalId:
        self.validation_calls.append((approval_id, experiment_id, plan_id, plan_hash, action))
        if self.validation_error is not None:
            raise self.validation_error
        return approval_id


class FakeResources:
    def __init__(self) -> None:
        self.reservations: list[JobAuthorizationDraft] = []

    def reserve(self, authorization: JobAuthorizationDraft) -> None:
        self.reservations.append(authorization)


class FakeDispatcher:
    def __init__(self) -> None:
        self.read_calls: list[AuthorizedReadOnlyCall] = []
        self.enqueue_calls: list[JobAuthorizationDraft] = []
        self.replay_calls: list[tuple[JobId, JobAuthorizationDraft]] = []
        self.persisted_jobs: dict[str, JobId] = {}

    def invoke_read_only(
        self,
        _registration: ToolRegistration,
        _arguments: BaseModel,
        authorization: AuthorizedReadOnlyCall,
    ) -> BaseModel:
        self.read_calls.append(authorization)
        return ReadResult(value="ok")

    def enqueue_job(
        self,
        _registration: ToolRegistration,
        _arguments: BaseModel,
        authorization: JobAuthorizationDraft,
    ) -> BaseModel:
        self.enqueue_calls.append(authorization)
        key = str(authorization.idempotency_key)
        job_id = self.persisted_jobs.get(key)
        created = job_id is None
        if job_id is None:
            job_id = JobId.new()
            self.persisted_jobs[key] = job_id
        return EnqueueResult(job_id=job_id, created=created)

    def replay_job(
        self,
        _registration: ToolRegistration,
        job_id: JobId,
        authorization: JobAuthorizationDraft,
    ) -> BaseModel:
        self.replay_calls.append((job_id, authorization))
        return EnqueueResult(job_id=job_id, created=False)


class FakeIdempotency:
    def __init__(self, dispatcher: FakeDispatcher) -> None:
        self.dispatcher = dispatcher
        self.calls: list[JobAuthorizationDraft] = []
        self.error: IdempotencyAuthorizationError | None = None

    def claim(self, authorization: JobAuthorizationDraft) -> JobIdempotencyClaim:
        self.calls.append(authorization)
        if self.error is not None:
            raise self.error
        return JobIdempotencyClaim(
            idempotency_key=authorization.idempotency_key,
            existing_job_id=self.dispatcher.persisted_jobs.get(str(authorization.idempotency_key)),
        )


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def append(self, event: AuditEvent) -> None:
        self.events.append(event)


@dataclass(slots=True)
class GatewayHarness:
    gateway: ToolGateway
    environment: GatewayEnvironment
    request: ToolCallRequest
    snapshot: ToolSetSnapshot
    registration: ToolRegistration
    policy: FakePolicy
    workflow: FakeWorkflow
    toolsets: FakeToolSets
    plans: FakePlans
    approvals: FakeApprovals
    idempotency: FakeIdempotency
    resources: FakeResources
    dispatcher: FakeDispatcher
    audit: FakeAudit


def budget(*, duration_seconds: int = 100) -> ExecutionBudget:
    return ExecutionBudget(
        max_duration_seconds=duration_seconds,
        max_requests=100,
        max_input_tokens=1_000,
        max_output_tokens=1_000,
        max_disk_growth_bytes=1_000,
    )


def policy_decision(
    *,
    allow: bool = True,
    reason: PolicyReasonCode = PolicyReasonCode.ALLOW,
    decision_id: str = "opa-execution-1",
) -> PolicyDecision:
    return PolicyDecision(
        decision_id=decision_id,
        allow=allow,
        reason_code=reason,
        requirements=PolicyRequirements(human_approval=True),
    )


def tool_definition() -> ToolDefinition:
    return ToolDefinition(
        name=START_DEPLOYMENT,
        input_schema_version="plan-execution-request/v1",
        risk_level=RiskLevel.L2,
        execution_mode=ToolExecutionMode.ASYNC_JOB,
        allowed_phases=(ExperimentPhase.DEPLOYMENT,),
        allowed_roles=(UserRole.OPERATOR,),
        required_hardware_capabilities=("gpu0",),
        required_providers=("vllm",),
        required_feature_flags=("deployment",),
        requires_plan=True,
    )


def approved_plan(
    *,
    experiment_id: ExperimentId,
    plan_id: PlanId,
    execution_budget: ExecutionBudget | None = None,
) -> PlanAuthorizationMaterial:
    return PlanAuthorizationMaterial(
        experiment_id=experiment_id,
        plan_id=plan_id,
        plan_hash=PLAN_HASH,
        status=PlanStatus.APPROVED,
        risk_level=RiskLevel.L2,
        execution_schema_version="deployment-execution/v1",
        budget=execution_budget or budget(),
    )


def approved_policy_approval(
    *,
    experiment_id: ExperimentId,
    plan_id: PlanId,
) -> PolicyApproval:
    return PolicyApproval(
        approval_id=ApprovalId.new(),
        experiment_id=experiment_id,
        plan_id=plan_id,
        plan_hash=PLAN_HASH,
        action=START_DEPLOYMENT,
        decision=ApprovalDecision.APPROVED,
        requester=PolicyHumanSubject(user_id=UserId.new(), role=UserRole.OPERATOR),
        decided_by=PolicyHumanSubject(user_id=UserId.new(), role=UserRole.ADMIN),
        expires_at=NOW + timedelta(hours=1),
    )


def make_harness(
    *,
    include_tool_in_snapshot: bool = True,
    plan_budget: ExecutionBudget | None = None,
    policy: FakePolicy | None = None,
) -> GatewayHarness:
    experiment_id = ExperimentId.new()
    plan_id = PlanId.new()
    subject = HumanSubject(user_id=UserId.new(), role=UserRole.OPERATOR)
    environment = GatewayEnvironment(
        experiment_id=experiment_id,
        subject=subject,
        hardware_capabilities=frozenset({"gpu0"}),
        enabled_providers=frozenset({"vllm"}),
        enabled_feature_flags=frozenset({"deployment"}),
    )
    definition = tool_definition()
    registration = ToolRegistration(definition, PlanExecutionRequest)
    context = VisibilityContext(
        experiment_id=experiment_id,
        subject=subject,
        phase=ExperimentPhase.DEPLOYMENT,
        hardware_capabilities=environment.hardware_capabilities,
        enabled_providers=environment.enabled_providers,
        enabled_feature_flags=environment.enabled_feature_flags,
    )
    entries = (
        (
            ToolSetEntry(
                name=definition.name,
                schema_version=definition.input_schema_version,
                risk_level=definition.risk_level,
            ),
        )
        if include_tool_in_snapshot
        else ()
    )
    snapshot = create_toolset_snapshot(
        context=context,
        tools=entries,
        policy_decision_ids=("opa-visibility-1",),
        created_at=NOW,
    )
    plan = approved_plan(
        experiment_id=experiment_id,
        plan_id=plan_id,
        execution_budget=plan_budget,
    )
    policy = policy or FakePolicy()
    workflow = FakeWorkflow(ExperimentPhase.DEPLOYMENT)
    toolsets = FakeToolSets(snapshot)
    plans = FakePlans(plan)
    approvals = FakeApprovals(
        approved_policy_approval(experiment_id=experiment_id, plan_id=plan_id)
    )
    resources = FakeResources()
    dispatcher = FakeDispatcher()
    idempotency = FakeIdempotency(dispatcher)
    audit = FakeAudit()
    gateway = ToolGateway(
        GatewayDependencies(
            registry=ToolRegistry((registration,)),
            policy=policy,
            workflow=workflow,
            toolsets=toolsets,
            plans=plans,
            approvals=approvals,
            idempotency=idempotency,
            resources=resources,
            dispatcher=dispatcher,
            audit=audit,
        ),
        budget_ceiling=budget(),
        clock=lambda: NOW,
    )
    arguments = PlanExecutionRequest(
        plan_id=plan_id,
        expected_plan_hash=PLAN_HASH,
    ).model_dump(mode="json")
    request = ToolCallRequest(
        request_id="gateway-request-1",
        tool_name=definition.name,
        tool_set_id=snapshot.tool_set_id,
        expected_tool_set_version=snapshot.tool_set_version,
        arguments=arguments,
    )
    return GatewayHarness(
        gateway=gateway,
        environment=environment,
        request=request,
        snapshot=snapshot,
        registration=registration,
        policy=policy,
        workflow=workflow,
        toolsets=toolsets,
        plans=plans,
        approvals=approvals,
        idempotency=idempotency,
        resources=resources,
        dispatcher=dispatcher,
        audit=audit,
    )


def assert_denied_without_dispatch(
    harness: GatewayHarness,
    expected_code: GatewayErrorCode,
    *,
    request: ToolCallRequest | None = None,
    environment: GatewayEnvironment | None = None,
) -> AuditEvent:
    invoked_request = request or harness.request
    invoked_environment = environment or harness.environment

    with pytest.raises(ToolGatewayError) as captured:
        harness.gateway.invoke(
            request=invoked_request,
            environment=invoked_environment,
        )

    assert captured.value.code is expected_code
    assert harness.resources.reservations == []
    assert harness.dispatcher.read_calls == []
    assert harness.dispatcher.enqueue_calls == []
    assert harness.dispatcher.replay_calls == []
    assert harness.dispatcher.persisted_jobs == {}
    assert len(harness.audit.events) == 1
    event = harness.audit.events[0]
    assert event.result is AuditResult.DENIED
    assert event.request_id == invoked_request.request_id
    assert event.action == str(invoked_request.tool_name)
    assert event.experiment_id == invoked_environment.experiment_id
    return event


def test_forged_unregistered_tool_is_audited_and_never_dispatched() -> None:
    harness = make_harness()
    forged = harness.request.model_copy(update={"tool_name": ToolName(root="start_optimization")})

    event = assert_denied_without_dispatch(
        harness,
        GatewayErrorCode.TOOL_NOT_REGISTERED,
        request=forged,
    )

    assert event.decision_id is None
    assert harness.workflow.calls == []
    assert harness.policy.calls == []


@pytest.mark.parametrize("variant", ["not_recorded", "version_mismatch", "missing_snapshot"])
def test_untrusted_toolset_reference_is_audited_and_never_dispatched(variant: str) -> None:
    harness = make_harness(include_tool_in_snapshot=variant != "not_recorded")
    expected_code = GatewayErrorCode.TOOL_NOT_VISIBLE
    request = harness.request
    if variant == "version_mismatch":
        request = request.model_copy(
            update={"expected_tool_set_version": Sha256Digest(root=f"sha256:{'f' * 64}")}
        )
    elif variant == "missing_snapshot":
        request = request.model_copy(update={"tool_set_id": ToolSetId.new()})
        expected_code = GatewayErrorCode.TOOL_SET_NOT_FOUND

    assert_denied_without_dispatch(harness, expected_code, request=request)

    assert harness.workflow.calls == []
    assert harness.policy.calls == []


def test_toolset_context_mismatch_is_audited_and_never_dispatched() -> None:
    harness = make_harness()
    changed_environment = harness.environment.model_copy(
        update={"enabled_feature_flags": frozenset()}
    )

    assert_denied_without_dispatch(
        harness,
        GatewayErrorCode.TOOL_SET_MISMATCH,
        environment=changed_environment,
    )

    assert harness.policy.calls == []


def test_stale_toolset_phase_is_audited_and_never_dispatched() -> None:
    harness = make_harness()
    harness.workflow.phase = ExperimentPhase.BENCHMARK

    assert_denied_without_dispatch(harness, GatewayErrorCode.TOOL_SET_MISMATCH)

    assert harness.policy.calls == []


def test_schema_rejection_is_audited_and_never_dispatched() -> None:
    harness = make_harness()
    malformed = harness.request.model_copy(update={"arguments": {}})

    assert_denied_without_dispatch(
        harness,
        GatewayErrorCode.SCHEMA_REJECTED,
        request=malformed,
    )

    assert harness.workflow.calls == []
    assert harness.plans.calls == []
    assert harness.policy.calls == []


def test_persisted_phase_failure_is_audited_and_never_dispatched() -> None:
    harness = make_harness()
    harness.workflow.error = WorkflowStateError("checkpoint unavailable")

    assert_denied_without_dispatch(harness, GatewayErrorCode.WORKFLOW_STATE_REJECTED)

    assert harness.plans.calls == []
    assert harness.policy.calls == []


def test_plan_rejection_is_audited_and_never_dispatched() -> None:
    harness = make_harness()
    harness.plans.error = PlanAuthorizationError("plan hash mismatch")

    assert_denied_without_dispatch(harness, GatewayErrorCode.PLAN_REJECTED)

    assert harness.policy.calls == []
    assert harness.approvals.candidate_calls == []


def test_budget_rejection_happens_before_opa_and_never_dispatches() -> None:
    harness = make_harness(plan_budget=budget(duration_seconds=101))

    assert_denied_without_dispatch(harness, GatewayErrorCode.BUDGET_EXCEEDED)

    assert harness.policy.calls == []
    assert harness.approvals.candidate_calls == []


@pytest.mark.parametrize("policy_error", [PolicyUnavailableError(), PolicyResponseError()])
def test_opa_failure_is_fail_closed_and_never_dispatches(
    policy_error: PolicyUnavailableError | PolicyResponseError,
) -> None:
    harness = make_harness(policy=FakePolicy(error=policy_error))

    event = assert_denied_without_dispatch(harness, GatewayErrorCode.POLICY_UNAVAILABLE)

    assert event.decision_id is None
    assert len(harness.policy.calls) == 1


def test_opa_denial_is_audited_with_decision_id_and_never_dispatched() -> None:
    decision = policy_decision(
        allow=False,
        reason=PolicyReasonCode.POLICY_DENIED,
        decision_id="opa-denied-1",
    )
    harness = make_harness(policy=FakePolicy(decision=decision))

    event = assert_denied_without_dispatch(harness, GatewayErrorCode.POLICY_DENIED)

    assert event.decision_id == decision.decision_id
    assert harness.approvals.validation_calls == []


def test_missing_approval_is_rejected_even_if_opa_allows_execution() -> None:
    harness = make_harness()
    harness.approvals.candidate = None

    event = assert_denied_without_dispatch(harness, GatewayErrorCode.APPROVAL_REQUIRED)

    assert event.decision_id == "opa-execution-1"
    assert harness.approvals.validation_calls == []


def test_opa_approval_required_reason_is_preserved_at_gateway_boundary() -> None:
    decision = policy_decision(
        allow=False,
        reason=PolicyReasonCode.APPROVAL_REQUIRED,
        decision_id="opa-approval-required-1",
    )
    harness = make_harness(policy=FakePolicy(decision=decision))
    harness.approvals.candidate = None

    event = assert_denied_without_dispatch(harness, GatewayErrorCode.APPROVAL_REQUIRED)

    assert event.decision_id == decision.decision_id
    assert harness.approvals.validation_calls == []


def test_invalid_approval_is_revalidated_after_opa_and_never_dispatched() -> None:
    harness = make_harness()
    harness.approvals.validation_error = ApprovalAuthorizationError(
        "approval expired before execution"
    )

    event = assert_denied_without_dispatch(harness, GatewayErrorCode.APPROVAL_REJECTED)

    assert event.decision_id == "opa-execution-1"
    assert len(harness.approvals.validation_calls) == 1


def test_approval_candidate_storage_failure_is_audited_before_opa() -> None:
    harness = make_harness()
    harness.approvals.candidate_error = ApprovalAuthorizationError("database unavailable")

    event = assert_denied_without_dispatch(
        harness,
        GatewayErrorCode.APPROVAL_REJECTED,
    )

    assert event.decision_id is None
    assert harness.policy.calls == []


def test_idempotency_conflict_is_rejected_before_resource_reservation() -> None:
    harness = make_harness()
    harness.idempotency.error = IdempotencyAuthorizationError("conflicting request")

    event = assert_denied_without_dispatch(
        harness,
        GatewayErrorCode.IDEMPOTENCY_CONFLICT,
    )

    assert event.decision_id == "opa-execution-1"
    assert len(harness.idempotency.calls) == 1


def test_repeated_async_request_uses_one_idempotent_persisted_job() -> None:
    harness = make_harness()

    first = harness.gateway.invoke(
        request=harness.request,
        environment=harness.environment,
    )
    second = harness.gateway.invoke(
        request=harness.request,
        environment=harness.environment,
    )

    assert isinstance(first, EnqueueResult)
    assert isinstance(second, EnqueueResult)
    assert first.created is True
    assert second.created is False
    assert first.job_id == second.job_id
    assert len(harness.dispatcher.persisted_jobs) == 1
    assert len(harness.dispatcher.enqueue_calls) == 1
    assert len(harness.dispatcher.replay_calls) == 1
    first_authorization = harness.dispatcher.enqueue_calls[0]
    replayed_job_id, second_authorization = harness.dispatcher.replay_calls[0]
    assert replayed_job_id == first.job_id
    assert first_authorization.request_hash == second_authorization.request_hash
    assert first_authorization.idempotency_key == second_authorization.idempotency_key
    assert first_authorization.tool_set_version == harness.snapshot.tool_set_version
    assert first_authorization.tool_set_id == harness.snapshot.tool_set_id
    assert first_authorization.approval_id == harness.approvals.candidate.approval_id
    assert first_authorization.policy_decision_id == "opa-execution-1"
    assert len(harness.resources.reservations) == 1
    assert [event.result for event in harness.audit.events] == [
        AuditResult.SUCCEEDED,
        AuditResult.SUCCEEDED,
    ]
