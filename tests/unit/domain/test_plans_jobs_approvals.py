from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from pydantic import ValidationError

from autopilot.domain.approvals import ApprovalRecord, validate_approval_for_execution
from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.enums import (
    ApprovalDecision,
    JobKind,
    JobStatus,
    PlanKind,
    RiskLevel,
)
from autopilot.domain.identifiers import (
    ApprovalId,
    ExperimentId,
    JobId,
    PlanHash,
    PlanId,
    ToolName,
    UserId,
)
from autopilot.domain.jobs import JobProgress, JobRecord, validate_job_transition
from autopilot.domain.plans import (
    ExecutionSpecification,
    PlanEnvelope,
    PlanExecutionRequest,
    compute_plan_envelope_hash,
)


class FakeExecutionSpecification(ExecutionSpecification):
    schema_version: Literal["test-plan/v1"] = "test-plan/v1"
    value: int


def make_specification(budget: ExecutionBudget) -> FakeExecutionSpecification:
    return FakeExecutionSpecification(
        provider="fake",
        provider_version="1.0.0",
        adapter_version="1.0.0",
        provider_profile_version="1",
        budget=budget,
        value=1,
    )


def test_plan_envelope_rejects_hash_mismatch(execution_budget: ExecutionBudget) -> None:
    specification = make_specification(execution_budget)

    with pytest.raises(ValidationError, match="plan hash"):
        PlanEnvelope[FakeExecutionSpecification](
            plan_id=PlanId.new(),
            experiment_id=ExperimentId.new(),
            kind=PlanKind.BENCHMARK,
            risk_level=RiskLevel.L2,
            execution_specification=specification,
            plan_hash=PlanHash(root=f"sha256:{'0' * 64}"),
            created_at=datetime.now(UTC),
        )


def test_plan_execution_request_has_no_mutable_execution_fields() -> None:
    schema_fields = PlanExecutionRequest.model_fields

    assert set(schema_fields) == {
        "schema_version",
        "plan_id",
        "expected_plan_hash",
    }

    with pytest.raises(ValidationError, match="idempotency_key"):
        PlanExecutionRequest(
            plan_id=PlanId.new(),
            expected_plan_hash=PlanHash(root=f"sha256:{'0' * 64}"),
            idempotency_key="caller-controlled-key",
        )


def test_plan_hash_covers_ids_kind_and_risk(execution_budget: ExecutionBudget) -> None:
    specification = make_specification(execution_budget)
    plan_id = PlanId.new()
    experiment_id = ExperimentId.new()
    baseline = compute_plan_envelope_hash(
        plan_id=plan_id,
        experiment_id=experiment_id,
        kind=PlanKind.BENCHMARK,
        risk_level=RiskLevel.L2,
        execution_specification=specification,
    )
    variants = (
        compute_plan_envelope_hash(
            plan_id=PlanId.new(),
            experiment_id=experiment_id,
            kind=PlanKind.BENCHMARK,
            risk_level=RiskLevel.L2,
            execution_specification=specification,
        ),
        compute_plan_envelope_hash(
            plan_id=plan_id,
            experiment_id=ExperimentId.new(),
            kind=PlanKind.BENCHMARK,
            risk_level=RiskLevel.L2,
            execution_specification=specification,
        ),
        compute_plan_envelope_hash(
            plan_id=plan_id,
            experiment_id=experiment_id,
            kind=PlanKind.DEPLOYMENT,
            risk_level=RiskLevel.L2,
            execution_specification=specification,
        ),
        compute_plan_envelope_hash(
            plan_id=plan_id,
            experiment_id=experiment_id,
            kind=PlanKind.BENCHMARK,
            risk_level=RiskLevel.L1,
            execution_specification=specification,
        ),
    )

    assert all(variant != baseline for variant in variants)


def test_job_state_machine_rejects_terminal_restart() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        validate_job_transition(JobStatus.SUCCEEDED, JobStatus.RUNNING)


def test_job_progress_rejects_completed_above_total() -> None:
    with pytest.raises(ValidationError, match="cannot exceed"):
        JobProgress(stage="benchmark", completed_units=2, total_units=1, latest_message="done")


def test_succeeded_job_requires_result_artifact() -> None:
    now = datetime.now(UTC)

    with pytest.raises(ValidationError, match="terminal data"):
        JobRecord(
            job_id=JobId.new(),
            experiment_id=ExperimentId.new(),
            plan_id=PlanId.new(),
            kind=JobKind.BENCHMARK,
            status=JobStatus.SUCCEEDED,
            submitted_at=now,
            started_at=now,
            ended_at=now,
        )


def test_job_rejects_data_that_conflicts_with_status(
    artifact_ref,
    error_envelope,
) -> None:
    now = datetime.now(UTC)
    common = {
        "job_id": JobId.new(),
        "experiment_id": ExperimentId.new(),
        "plan_id": PlanId.new(),
        "kind": JobKind.BENCHMARK,
        "submitted_at": now,
    }

    with pytest.raises(ValidationError, match="terminal data"):
        JobRecord(status=JobStatus.QUEUED, result_artifact=artifact_ref, **common)
    with pytest.raises(ValidationError, match="terminal data"):
        JobRecord(status=JobStatus.QUEUED, error=error_envelope, **common)
    with pytest.raises(ValidationError, match="terminal data"):
        JobRecord(
            status=JobStatus.SUCCEEDED,
            started_at=now,
            ended_at=now,
            result_artifact=artifact_ref,
            error=error_envelope,
            **common,
        )
    with pytest.raises(ValidationError, match="terminal data"):
        JobRecord(status=JobStatus.TIMED_OUT, started_at=now, ended_at=now, **common)


def test_running_job_rejects_started_time_before_submission() -> None:
    now = datetime.now(UTC)

    with pytest.raises(ValidationError, match="chronological"):
        JobRecord(
            job_id=JobId.new(),
            experiment_id=ExperimentId.new(),
            plan_id=PlanId.new(),
            kind=JobKind.BENCHMARK,
            status=JobStatus.RUNNING,
            submitted_at=now,
            started_at=now - timedelta(seconds=1),
        )


def test_approval_validation_binds_hash_and_expiry(
    execution_budget: ExecutionBudget,
) -> None:
    now = datetime.now(UTC)
    specification = make_specification(execution_budget)
    plan_id = PlanId.new()
    plan_hash = compute_plan_envelope_hash(
        plan_id=plan_id,
        experiment_id=ExperimentId.new(),
        kind=PlanKind.BENCHMARK,
        risk_level=RiskLevel.L2,
        execution_specification=specification,
    )
    approval = ApprovalRecord(
        approval_id=ApprovalId.new(),
        plan_id=plan_id,
        plan_hash=plan_hash,
        action=ToolName(root="start_benchmark"),
        requester_id=UserId.new(),
        requested_at=now,
        expires_at=now + timedelta(minutes=10),
        decision=ApprovalDecision.APPROVED,
        decided_by=UserId.new(),
        decided_at=now,
    )

    validate_approval_for_execution(
        approval,
        approval.plan_id,
        plan_hash,
        approval.action,
        now + timedelta(minutes=1),
    )

    with pytest.raises(ValueError, match="expired"):
        validate_approval_for_execution(
            approval,
            approval.plan_id,
            plan_hash,
            approval.action,
            now + timedelta(minutes=11),
        )

    with pytest.raises(ValueError, match="hash"):
        validate_approval_for_execution(
            approval,
            approval.plan_id,
            PlanHash(root=f"sha256:{'f' * 64}"),
            approval.action,
            now + timedelta(minutes=1),
        )

    with pytest.raises(ValueError, match="plan ID"):
        validate_approval_for_execution(
            approval,
            PlanId.new(),
            plan_hash,
            approval.action,
            now + timedelta(minutes=1),
        )

    with pytest.raises(ValueError, match="action"):
        validate_approval_for_execution(
            approval,
            approval.plan_id,
            plan_hash,
            ToolName(root="start_deployment"),
            now + timedelta(minutes=1),
        )

    with pytest.raises(ValueError, match="timezone-aware"):
        validate_approval_for_execution(
            approval,
            approval.plan_id,
            plan_hash,
            approval.action,
            datetime(2026, 8, 5, 12, 0),  # noqa: DTZ001
        )


def test_valid_plan_envelope_accepts_canonical_hash(execution_budget: ExecutionBudget) -> None:
    specification = make_specification(execution_budget)
    plan_id = PlanId.new()
    experiment_id = ExperimentId.new()
    kind = PlanKind.BENCHMARK
    risk_level = RiskLevel.L2
    envelope = PlanEnvelope[FakeExecutionSpecification](
        plan_id=plan_id,
        experiment_id=experiment_id,
        kind=kind,
        risk_level=risk_level,
        execution_specification=specification,
        plan_hash=compute_plan_envelope_hash(
            plan_id=plan_id,
            experiment_id=experiment_id,
            kind=kind,
            risk_level=risk_level,
            execution_specification=specification,
        ),
        created_at=datetime.now(UTC),
    )

    assert envelope.execution_specification.value == 1
