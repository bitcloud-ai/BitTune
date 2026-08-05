from datetime import timedelta

import pytest
from pydantic import ValidationError

from autopilot.domain.enums import ApprovalDecision, UserRole
from autopilot.domain.identifiers import (
    ApprovalId,
    ExperimentId,
    PlanHash,
    PlanId,
    ToolName,
    UserId,
)
from autopilot.domain.identities import HumanSubject
from autopilot.gateway.approval_ports import CreateApprovalRequest, DecideApprovalRequest


def _human(role: UserRole) -> HumanSubject:
    return HumanSubject(user_id=UserId.new(), role=role)


def test_create_approval_request_rejects_viewer() -> None:
    with pytest.raises(ValidationError, match="operator or admin"):
        CreateApprovalRequest(
            approval_id=ApprovalId.new(),
            experiment_id=ExperimentId.new(),
            plan_id=PlanId.new(),
            expected_plan_hash=PlanHash(root=f"sha256:{'1' * 64}"),
            action=ToolName(root="start_deployment"),
            requester=_human(UserRole.VIEWER),
            expires_in=timedelta(minutes=15),
        )
    with pytest.raises(ValidationError):
        CreateApprovalRequest.model_validate(
            {
                "approval_id": ApprovalId.new(),
                "experiment_id": ExperimentId.new(),
                "plan_id": PlanId.new(),
                "expected_plan_hash": PlanHash(root=f"sha256:{'3' * 64}"),
                "action": ToolName(root="start_deployment"),
                "requester": {
                    "kind": "service",
                    "service_name": "autopilot-api",
                },
                "expires_in": timedelta(minutes=15),
            }
        )


def test_decide_approval_request_requires_human_admin() -> None:
    common = {
        "approval_id": ApprovalId.new(),
        "experiment_id": ExperimentId.new(),
        "expected_plan_id": PlanId.new(),
        "expected_plan_hash": PlanHash(root=f"sha256:{'2' * 64}"),
        "expected_action": ToolName(root="start_deployment"),
        "decision": ApprovalDecision.APPROVED,
    }

    with pytest.raises(ValidationError, match="human admin"):
        DecideApprovalRequest(actor=_human(UserRole.OPERATOR), **common)
    with pytest.raises(ValidationError):
        DecideApprovalRequest.model_validate(
            {
                **common,
                "actor": {
                    "kind": "service",
                    "service_name": "autopilot-api",
                },
            }
        )
