from datetime import UTC, datetime

import httpx
import pytest

from autopilot.domain.enums import ExperimentPhase, RiskLevel, UserRole
from autopilot.domain.identifiers import ToolName, UserId
from autopilot.policy.models import (
    PolicyEvaluationPurpose,
    PolicyHumanSubject,
    PolicyInput,
    PolicyReasonCode,
    PolicyTool,
)
from autopilot.policy.opa import (
    OPA_DECISION_PATH,
    OpaPolicyClient,
    PolicyResponseError,
    PolicyUnavailableError,
)

CONNECTION_REFUSED = "connection refused"


def policy_input() -> PolicyInput:
    return PolicyInput(
        request_id="request-1",
        purpose=PolicyEvaluationPurpose.VISIBILITY,
        current_time=datetime(2026, 8, 6, tzinfo=UTC),
        phase=ExperimentPhase.BENCHMARK,
        subject=PolicyHumanSubject(
            kind="human",
            user_id=UserId.new(),
            role=UserRole.VIEWER,
        ),
        tool=PolicyTool(
            name=ToolName(root="get_benchmark_result"),
            schema_version="job-query/v1",
            risk_level=RiskLevel.L0,
            allowed_phases=(ExperimentPhase.BENCHMARK,),
            allowed_roles=(UserRole.VIEWER,),
            environment_supported=True,
            provider_enabled=True,
            feature_flags_enabled=True,
        ),
    )


def client_with(handler) -> OpaPolicyClient:
    transport = httpx.MockTransport(handler)
    return OpaPolicyClient(httpx.Client(base_url="http://opa.test", transport=transport))


def test_opa_client_posts_only_typed_input_to_fixed_document() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = request.read().decode()
        return httpx.Response(
            200,
            json={
                "decision_id": "decision-1",
                "result": {
                    "allow": True,
                    "reason_code": "ALLOW",
                    "requirements": {"human_approval": False},
                },
            },
        )

    decision = client_with(handler).evaluate(policy_input())

    assert captured["path"] == OPA_DECISION_PATH
    assert "authorization" not in str(captured["body"]).lower()
    assert decision.allow is True
    assert decision.reason_code is PolicyReasonCode.ALLOW
    assert decision.decision_id == "decision-1"


@pytest.mark.parametrize("status_code", [400, 500, 503])
def test_opa_client_fails_closed_on_non_success(status_code: int) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "untrusted detail"})

    with pytest.raises(PolicyUnavailableError, match="unavailable"):
        client_with(handler).evaluate(policy_input())


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json={"result": {"allow": True}}),
        httpx.Response(200, json={"decision_id": "decision-1"}),
        httpx.Response(
            200,
            json={
                "decision_id": "decision-1",
                "result": {
                    "allow": True,
                    "reason_code": "UNKNOWN",
                    "requirements": {"human_approval": False},
                },
            },
        ),
    ],
)
def test_opa_client_fails_closed_on_invalid_response(response: httpx.Response) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return response

    with pytest.raises(PolicyResponseError, match="invalid"):
        client_with(handler).evaluate(policy_input())


def test_opa_client_fails_closed_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(CONNECTION_REFUSED, request=request)

    with pytest.raises(PolicyUnavailableError, match="unavailable"):
        client_with(handler).evaluate(policy_input())
