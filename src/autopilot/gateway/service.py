"""Mandatory Tool Gateway enforcement pipeline for every Agent-visible action."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn

from pydantic import BaseModel, ValidationError

from autopilot.domain.base import utc_now
from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.enums import ExperimentPhase, PlanStatus, RiskLevel
from autopilot.domain.hashing import compute_content_hash
from autopilot.domain.identifiers import ApprovalId, AuditEventId
from autopilot.domain.identities import HumanSubject, ServiceSubject
from autopilot.domain.plans import PlanExecutionRequest
from autopilot.gateway.errors import (
    ApprovalAuthorizationError,
    GatewayErrorCode,
    IdempotencyAuthorizationError,
    PlanAuthorizationError,
    ResourceUnavailableError,
    ToolDispatchError,
    ToolGatewayError,
    WorkflowStateError,
)
from autopilot.gateway.models import (
    AuthorizedReadOnlyCall,
    GatewayEnvironment,
    IdempotencyKeyMaterial,
    JobAuthorizationDraft,
    PlanAuthorizationMaterial,
    ToolCallRequest,
    ToolExecutionMode,
    ToolSetSnapshot,
    VisibilityContext,
)
from autopilot.gateway.ports import (
    ApprovalAuthorizationPort,
    GatewayAuditSink,
    JobIdempotencyPort,
    PlanAuthorizationRepository,
    ResourceReservationPort,
    ToolDispatcher,
    ToolSetSnapshotRepository,
    WorkflowStateReader,
)
from autopilot.gateway.registry import ToolRegistration, ToolRegistry
from autopilot.jobs.models import AuditEvent, AuditResult
from autopilot.policy.models import (
    PolicyApproval,
    PolicyBudget,
    PolicyDecision,
    PolicyEvaluationPurpose,
    PolicyHumanSubject,
    PolicyInput,
    PolicyPlan,
    PolicyReasonCode,
    PolicyServiceSubject,
    PolicyTool,
)
from autopilot.policy.opa import PolicyResponseError, PolicyUnavailableError
from autopilot.policy.ports import PolicyClient

TOOL_NOT_REGISTERED = "requested Tool is not registered"
TOOL_NOT_VISIBLE = "requested Tool is not visible in the recorded Tool Set"
TOOL_SET_NOT_FOUND = "recorded Tool Set does not exist"
TOOL_SET_MISMATCH = "recorded Tool Set does not match the current trusted context"
SCHEMA_REJECTED = "Tool arguments failed Schema validation"
WORKFLOW_STATE_REJECTED = "persisted workflow state does not allow the Tool"
PLAN_REJECTED = "persisted Plan cannot authorize this execution"
BUDGET_EXCEEDED = "persisted Plan budget exceeds the configured policy ceiling"
POLICY_UNAVAILABLE = "authorization policy could not produce a trusted decision"
POLICY_DENIED = "authorization policy denied the Tool call"
APPROVAL_REQUIRED = "a matching independent Approval is required"
APPROVAL_REJECTED = "persisted Approval is not valid for this execution"
IDEMPOTENCY_CONFLICT = "idempotency key is bound to different immutable input"
RESOURCE_UNAVAILABLE = "required resource could not be reserved"
DISPATCH_FAILED = "registered capability service rejected the Tool call"


@dataclass(frozen=True, slots=True)
class GatewayDependencies:
    registry: ToolRegistry
    policy: PolicyClient
    workflow: WorkflowStateReader
    toolsets: ToolSetSnapshotRepository
    plans: PlanAuthorizationRepository
    approvals: ApprovalAuthorizationPort
    idempotency: JobIdempotencyPort
    resources: ResourceReservationPort
    dispatcher: ToolDispatcher
    audit: GatewayAuditSink


@dataclass(frozen=True, slots=True)
class _Invocation:
    request: ToolCallRequest
    environment: GatewayEnvironment
    now: datetime


@dataclass(frozen=True, slots=True)
class _Authorization:
    plan: PlanAuthorizationMaterial | None
    approval_id: ApprovalId | None
    policy_decision: PolicyDecision


@dataclass(frozen=True, slots=True)
class _PolicyContext:
    registration: ToolRegistration
    phase: ExperimentPhase
    plan: PlanAuthorizationMaterial | None
    approval: PolicyApproval | None


def _actor(subject: HumanSubject | ServiceSubject) -> str:
    if isinstance(subject, HumanSubject):
        return str(subject.user_id)
    return f"service:{subject.service_name}"


def _policy_subject(
    subject: HumanSubject | ServiceSubject,
) -> PolicyHumanSubject | PolicyServiceSubject:
    if isinstance(subject, HumanSubject):
        return PolicyHumanSubject.model_validate(subject.model_dump(mode="json"))
    return PolicyServiceSubject.model_validate(subject.model_dump(mode="json"))


def _budget_within_ceiling(requested: ExecutionBudget, ceiling: ExecutionBudget) -> bool:
    return (
        requested.max_duration_seconds <= ceiling.max_duration_seconds
        and requested.max_requests <= ceiling.max_requests
        and requested.max_input_tokens <= ceiling.max_input_tokens
        and requested.max_output_tokens <= ceiling.max_output_tokens
        and requested.max_disk_growth_bytes <= ceiling.max_disk_growth_bytes
    )


def _snapshot_contains(snapshot: ToolSetSnapshot, registration: ToolRegistration) -> bool:
    definition = registration.definition
    return any(
        entry.name == definition.name
        and entry.schema_version == definition.input_schema_version
        and entry.risk_level is definition.risk_level
        for entry in snapshot.tools
    )


def _snapshot_matches_environment(
    snapshot: ToolSetSnapshot,
    environment: GatewayEnvironment,
    phase: ExperimentPhase,
) -> bool:
    return (
        snapshot.experiment_id == environment.experiment_id
        and snapshot.subject == environment.subject
        and snapshot.phase is phase
        and frozenset(snapshot.hardware_capabilities) == environment.hardware_capabilities
        and frozenset(snapshot.enabled_providers) == environment.enabled_providers
        and frozenset(snapshot.enabled_feature_flags) == environment.enabled_feature_flags
    )


class ToolGateway:
    """Enforce visibility, validation, authorization, dispatch, and audit in order."""

    def __init__(
        self,
        dependencies: GatewayDependencies,
        *,
        budget_ceiling: ExecutionBudget,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._dependencies = dependencies
        self._budget_ceiling = budget_ceiling
        self._clock = clock

    def available_tools(
        self,
        *,
        environment: GatewayEnvironment,
        request_id: str,
    ) -> ToolSetSnapshot:
        """Resolve and persist the exact Tool Set before an LLM call."""
        phase = self._dependencies.workflow.current_phase(environment.experiment_id)
        snapshot = self._dependencies.registry.resolve_visible_tools(
            context=VisibilityContext(
                experiment_id=environment.experiment_id,
                subject=environment.subject,
                phase=phase,
                hardware_capabilities=environment.hardware_capabilities,
                enabled_providers=environment.enabled_providers,
                enabled_feature_flags=environment.enabled_feature_flags,
            ),
            request_id=request_id,
            current_time=self._clock(),
            policy=self._dependencies.policy,
        )
        self._dependencies.toolsets.add(snapshot)
        return snapshot

    def invoke(
        self,
        *,
        request: ToolCallRequest,
        environment: GatewayEnvironment,
    ) -> BaseModel:
        invocation = _Invocation(request=request, environment=environment, now=self._clock())
        registration, snapshot = self._visibility_stage(invocation)
        arguments = self._schema_stage(invocation, registration)
        phase = self._workflow_stage(invocation, snapshot)
        authorization = self._authorization_stages(
            invocation,
            registration,
            arguments,
            phase,
        )
        result = self._dispatch_stage(
            invocation,
            registration,
            snapshot,
            arguments,
            authorization,
        )
        self._audit_event(
            invocation,
            result=AuditResult.SUCCEEDED,
            decision_id=authorization.policy_decision.decision_id,
        )
        return result

    def _visibility_stage(
        self,
        invocation: _Invocation,
    ) -> tuple[ToolRegistration, ToolSetSnapshot]:
        registration = self._dependencies.registry.get(invocation.request.tool_name)
        if registration is None:
            self._reject(invocation, GatewayErrorCode.TOOL_NOT_REGISTERED, TOOL_NOT_REGISTERED)
        snapshot = self._dependencies.toolsets.get(invocation.request.tool_set_id)
        if snapshot is None:
            self._reject(invocation, GatewayErrorCode.TOOL_SET_NOT_FOUND, TOOL_SET_NOT_FOUND)
        if (
            snapshot.tool_set_version != invocation.request.expected_tool_set_version
            or not _snapshot_contains(snapshot, registration)
        ):
            self._reject(invocation, GatewayErrorCode.TOOL_NOT_VISIBLE, TOOL_NOT_VISIBLE)
        return registration, snapshot

    def _schema_stage(
        self,
        invocation: _Invocation,
        registration: ToolRegistration,
    ) -> BaseModel:
        try:
            return registration.input_model.model_validate(invocation.request.arguments)
        except ValidationError:
            self._reject(invocation, GatewayErrorCode.SCHEMA_REJECTED, SCHEMA_REJECTED)

    def _workflow_stage(
        self,
        invocation: _Invocation,
        snapshot: ToolSetSnapshot,
    ) -> ExperimentPhase:
        try:
            phase = self._dependencies.workflow.current_phase(invocation.environment.experiment_id)
        except WorkflowStateError:
            self._reject(
                invocation,
                GatewayErrorCode.WORKFLOW_STATE_REJECTED,
                WORKFLOW_STATE_REJECTED,
            )
        if not _snapshot_matches_environment(snapshot, invocation.environment, phase):
            self._reject(invocation, GatewayErrorCode.TOOL_SET_MISMATCH, TOOL_SET_MISMATCH)
        return phase

    def _authorization_stages(
        self,
        invocation: _Invocation,
        registration: ToolRegistration,
        arguments: BaseModel,
        phase: ExperimentPhase,
    ) -> _Authorization:
        plan = self._load_plan(invocation, registration, arguments)
        if plan is not None and not _budget_within_ceiling(plan.budget, self._budget_ceiling):
            self._reject(invocation, GatewayErrorCode.BUDGET_EXCEEDED, BUDGET_EXCEEDED)

        approval = self._approval_candidate(invocation, registration, plan)
        policy_decision = self._evaluate_execution_policy(
            invocation,
            _PolicyContext(
                registration=registration,
                phase=phase,
                plan=plan,
                approval=approval,
            ),
        )
        if not policy_decision.allow:
            self._reject_policy_decision(invocation, policy_decision)
        approval_id = self._require_approval(
            invocation,
            registration,
            plan,
            approval,
            policy_decision,
        )
        return _Authorization(
            plan=plan,
            approval_id=approval_id,
            policy_decision=policy_decision,
        )

    def _load_plan(
        self,
        invocation: _Invocation,
        registration: ToolRegistration,
        arguments: BaseModel,
    ) -> PlanAuthorizationMaterial | None:
        if not registration.definition.requires_plan:
            return None
        if not isinstance(arguments, PlanExecutionRequest):
            self._reject(invocation, GatewayErrorCode.PLAN_REJECTED, PLAN_REJECTED)
        try:
            plan = self._dependencies.plans.get_for_execution(
                experiment_id=invocation.environment.experiment_id,
                plan_id=arguments.plan_id,
                expected_plan_hash=arguments.expected_plan_hash,
            )
        except PlanAuthorizationError:
            self._reject(invocation, GatewayErrorCode.PLAN_REJECTED, PLAN_REJECTED)
        if (
            plan.status is not PlanStatus.APPROVED
            or plan.risk_level is not registration.definition.risk_level
        ):
            self._reject(invocation, GatewayErrorCode.PLAN_REJECTED, PLAN_REJECTED)
        return plan

    def _approval_candidate(
        self,
        invocation: _Invocation,
        registration: ToolRegistration,
        plan: PlanAuthorizationMaterial | None,
    ) -> PolicyApproval | None:
        if registration.definition.risk_level is not RiskLevel.L2 or plan is None:
            return None
        try:
            return self._dependencies.approvals.get_candidate(
                experiment_id=invocation.environment.experiment_id,
                plan_id=plan.plan_id,
                plan_hash=plan.plan_hash,
                action=registration.definition.name,
            )
        except ApprovalAuthorizationError:
            self._reject(
                invocation,
                GatewayErrorCode.APPROVAL_REJECTED,
                APPROVAL_REJECTED,
            )

    def _evaluate_execution_policy(
        self,
        invocation: _Invocation,
        context: _PolicyContext,
    ) -> PolicyDecision:
        definition = context.registration.definition
        environment = invocation.environment
        try:
            return self._dependencies.policy.evaluate(
                PolicyInput(
                    request_id=invocation.request.request_id,
                    purpose=PolicyEvaluationPurpose.EXECUTION,
                    current_time=invocation.now,
                    phase=context.phase,
                    subject=_policy_subject(environment.subject),
                    tool=PolicyTool(
                        name=definition.name,
                        schema_version=definition.input_schema_version,
                        risk_level=definition.risk_level,
                        allowed_phases=tuple(sorted(definition.allowed_phases, key=str)),
                        allowed_roles=tuple(sorted(definition.allowed_roles, key=str)),
                        environment_supported=set(
                            definition.required_hardware_capabilities
                        ).issubset(environment.hardware_capabilities),
                        provider_enabled=set(definition.required_providers).issubset(
                            environment.enabled_providers
                        ),
                        feature_flags_enabled=set(definition.required_feature_flags).issubset(
                            environment.enabled_feature_flags
                        ),
                    ),
                    plan=(
                        PolicyPlan(
                            experiment_id=context.plan.experiment_id,
                            plan_id=context.plan.plan_id,
                            plan_hash=context.plan.plan_hash,
                            risk_level=context.plan.risk_level,
                        )
                        if context.plan is not None
                        else None
                    ),
                    approval=context.approval,
                    budget=(
                        PolicyBudget(
                            requested=context.plan.budget,
                            ceiling=self._budget_ceiling,
                        )
                        if context.plan is not None
                        else None
                    ),
                )
            )
        except (PolicyUnavailableError, PolicyResponseError):
            self._reject(
                invocation,
                GatewayErrorCode.POLICY_UNAVAILABLE,
                POLICY_UNAVAILABLE,
            )

    def _reject_policy_decision(
        self,
        invocation: _Invocation,
        decision: PolicyDecision,
    ) -> NoReturn:
        approval_missing = decision.reason_code is PolicyReasonCode.APPROVAL_REQUIRED
        code = (
            GatewayErrorCode.APPROVAL_REQUIRED
            if approval_missing
            else GatewayErrorCode.POLICY_DENIED
        )
        message = APPROVAL_REQUIRED if approval_missing else POLICY_DENIED
        self._reject(
            invocation,
            code,
            message,
            decision_id=decision.decision_id,
        )

    def _require_approval(
        self,
        invocation: _Invocation,
        registration: ToolRegistration,
        plan: PlanAuthorizationMaterial | None,
        approval: PolicyApproval | None,
        decision: PolicyDecision,
    ) -> ApprovalId | None:
        if registration.definition.risk_level is not RiskLevel.L2:
            return None
        if plan is None or approval is None:
            self._reject(
                invocation,
                GatewayErrorCode.APPROVAL_REQUIRED,
                APPROVAL_REQUIRED,
                decision_id=decision.decision_id,
            )
        try:
            return self._dependencies.approvals.require_valid_for_execution(
                approval_id=approval.approval_id,
                experiment_id=invocation.environment.experiment_id,
                plan_id=plan.plan_id,
                plan_hash=plan.plan_hash,
                action=registration.definition.name,
            )
        except ApprovalAuthorizationError:
            self._reject(
                invocation,
                GatewayErrorCode.APPROVAL_REJECTED,
                APPROVAL_REJECTED,
                decision_id=decision.decision_id,
            )

    def _dispatch_stage(
        self,
        invocation: _Invocation,
        registration: ToolRegistration,
        snapshot: ToolSetSnapshot,
        arguments: BaseModel,
        authorization: _Authorization,
    ) -> BaseModel:
        request_hash = compute_content_hash(arguments)
        idempotency_key = compute_content_hash(
            IdempotencyKeyMaterial(
                experiment_id=invocation.environment.experiment_id,
                action=registration.definition.name,
                tool_schema_version=registration.definition.input_schema_version,
                request_hash=request_hash,
            )
        )
        try:
            if registration.definition.execution_mode is ToolExecutionMode.READ_ONLY:
                return self._dependencies.dispatcher.invoke_read_only(
                    registration,
                    arguments,
                    AuthorizedReadOnlyCall(
                        experiment_id=invocation.environment.experiment_id,
                        subject=invocation.environment.subject,
                        action=registration.definition.name,
                        tool_schema_version=registration.definition.input_schema_version,
                        tool_set_id=snapshot.tool_set_id,
                        tool_set_version=snapshot.tool_set_version,
                        policy_decision_id=authorization.policy_decision.decision_id,
                        request_hash=request_hash,
                        authorized_at=invocation.now,
                    ),
                )
            if authorization.plan is None:
                self._reject(invocation, GatewayErrorCode.PLAN_REJECTED, PLAN_REJECTED)
            job_authorization = JobAuthorizationDraft(
                experiment_id=invocation.environment.experiment_id,
                subject=invocation.environment.subject,
                action=registration.definition.name,
                risk_level=registration.definition.risk_level,
                plan_id=authorization.plan.plan_id,
                plan_hash=authorization.plan.plan_hash,
                approval_id=authorization.approval_id,
                tool_schema_version=registration.definition.input_schema_version,
                tool_set_id=snapshot.tool_set_id,
                tool_set_version=snapshot.tool_set_version,
                policy_decision_id=authorization.policy_decision.decision_id,
                request_hash=request_hash,
                idempotency_key=idempotency_key,
                authorized_at=invocation.now,
            )
            try:
                claim = self._dependencies.idempotency.claim(job_authorization)
            except IdempotencyAuthorizationError:
                self._reject(
                    invocation,
                    GatewayErrorCode.IDEMPOTENCY_CONFLICT,
                    IDEMPOTENCY_CONFLICT,
                    decision_id=authorization.policy_decision.decision_id,
                )
            if claim.existing_job_id is not None:
                return self._dependencies.dispatcher.replay_job(
                    registration,
                    claim.existing_job_id,
                    job_authorization,
                )
            self._dependencies.resources.reserve(job_authorization)
            return self._dependencies.dispatcher.enqueue_job(
                registration,
                arguments,
                job_authorization,
            )
        except ResourceUnavailableError:
            self._reject(
                invocation,
                GatewayErrorCode.RESOURCE_UNAVAILABLE,
                RESOURCE_UNAVAILABLE,
                decision_id=authorization.policy_decision.decision_id,
            )
        except ToolDispatchError:
            self._fail(invocation, authorization.policy_decision.decision_id)

    def _reject(
        self,
        invocation: _Invocation,
        code: GatewayErrorCode,
        message: str,
        *,
        decision_id: str | None = None,
    ) -> NoReturn:
        self._audit_event(
            invocation,
            result=AuditResult.DENIED,
            decision_id=decision_id,
        )
        raise ToolGatewayError(code, message)

    def _fail(self, invocation: _Invocation, decision_id: str) -> NoReturn:
        self._audit_event(
            invocation,
            result=AuditResult.FAILED,
            decision_id=decision_id,
        )
        raise ToolGatewayError(GatewayErrorCode.DISPATCH_FAILED, DISPATCH_FAILED)

    def _audit_event(
        self,
        invocation: _Invocation,
        *,
        result: AuditResult,
        decision_id: str | None,
    ) -> None:
        environment = invocation.environment
        request = invocation.request
        self._dependencies.audit.append(
            AuditEvent(
                audit_event_id=AuditEventId.new(),
                experiment_id=environment.experiment_id,
                actor=_actor(environment.subject),
                action=str(request.tool_name),
                resource_type="tool_call",
                resource_id=str(environment.experiment_id),
                request_id=request.request_id,
                decision_id=decision_id,
                result=result,
                occurred_at=invocation.now,
            )
        )
