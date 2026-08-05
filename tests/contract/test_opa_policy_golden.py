from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = REPOSITORY_ROOT / "policies" / "autopilot.rego"
OPA_PATH = shutil.which("opa")
OPA_REQUIRED = "OPA executable is required for the Rego Policy Golden Test"

USER_REQUESTER = "user_11111111111111111111111111111111"
USER_ADMIN = "user_22222222222222222222222222222222"
PLAN_ID = "plan_11111111111111111111111111111111"
EXPERIMENT_ID = "exp_11111111111111111111111111111111"
APPROVAL_ID = "approval_11111111111111111111111111111111"
PLAN_HASH = "sha256:" + "1" * 64


def _budget(value: int = 100) -> dict[str, object]:
    return {
        "schema_version": "execution-budget/v1",
        "max_duration_seconds": value,
        "max_requests": value,
        "max_input_tokens": value,
        "max_output_tokens": value,
        "max_disk_growth_bytes": value,
    }


def _l2_input() -> dict[str, object]:
    return {
        "schema_version": "policy-input/v1",
        "request_id": "policy-golden",
        "purpose": "execution",
        "current_time": "2026-08-06T00:00:00Z",
        "phase": "benchmark",
        "subject": {"kind": "human", "user_id": USER_REQUESTER, "role": "operator"},
        "tool": {
            "name": "start_benchmark",
            "schema_version": "plan-execution-request/v1",
            "risk_level": "L2",
            "allowed_phases": ["benchmark"],
            "allowed_roles": ["admin", "operator"],
            "environment_supported": True,
            "provider_enabled": True,
            "feature_flags_enabled": True,
        },
        "plan": {
            "experiment_id": EXPERIMENT_ID,
            "plan_id": PLAN_ID,
            "plan_hash": PLAN_HASH,
            "risk_level": "L2",
        },
        "approval": {
            "approval_id": APPROVAL_ID,
            "experiment_id": EXPERIMENT_ID,
            "plan_id": PLAN_ID,
            "plan_hash": PLAN_HASH,
            "action": "start_benchmark",
            "decision": "approved",
            "requester": {
                "kind": "human",
                "user_id": USER_REQUESTER,
                "role": "operator",
            },
            "decided_by": {"kind": "human", "user_id": USER_ADMIN, "role": "admin"},
            "expires_at": "2026-08-06T00:10:00Z",
        },
        "budget": {"requested": _budget(), "ceiling": _budget()},
    }


def _cases() -> Iterator[pytest.ParamSpec]:
    l0 = _l2_input()
    l0["tool"] = {
        **l0["tool"],
        "name": "get_benchmark_result",
        "schema_version": "job-query/v1",
        "risk_level": "L0",
        "allowed_roles": ["admin", "operator", "viewer"],
    }
    l0["subject"] = {"kind": "human", "user_id": USER_REQUESTER, "role": "viewer"}
    l0["plan"] = None
    l0["approval"] = None
    l0["budget"] = None
    yield pytest.param(l0, True, "ALLOW", id="l0-allow")

    matching = _l2_input()
    yield pytest.param(matching, True, "ALLOW", id="l2-matching-approval")

    missing = _l2_input()
    missing["approval"] = None
    yield pytest.param(missing, False, "APPROVAL_REQUIRED", id="l2-approval-required")

    self_approved = deepcopy(matching)
    self_approved["approval"]["decided_by"]["user_id"] = USER_REQUESTER
    yield pytest.param(
        self_approved,
        False,
        "APPROVAL_IDENTITY_DENIED",
        id="self-approval-denied",
    )

    service_approved = deepcopy(matching)
    service_approved["approval"]["decided_by"] = {
        "kind": "service",
        "service_name": "autopilot-worker",
    }
    yield pytest.param(
        service_approved,
        False,
        "APPROVAL_IDENTITY_DENIED",
        id="service-approval-denied",
    )

    operator_approved = deepcopy(matching)
    operator_approved["approval"]["decided_by"]["role"] = "operator"
    yield pytest.param(
        operator_approved,
        False,
        "APPROVAL_IDENTITY_DENIED",
        id="non-admin-approval-denied",
    )

    expired = deepcopy(matching)
    expired["approval"]["expires_at"] = "2026-08-06T00:00:00Z"
    yield pytest.param(expired, False, "APPROVAL_EXPIRED", id="expired-approval-denied")

    mismatched = deepcopy(matching)
    mismatched["approval"]["plan_hash"] = "sha256:" + "2" * 64
    yield pytest.param(mismatched, False, "APPROVAL_MISMATCH", id="hash-mismatch-denied")

    for field in (
        "max_duration_seconds",
        "max_requests",
        "max_input_tokens",
        "max_output_tokens",
        "max_disk_growth_bytes",
    ):
        over_budget = deepcopy(matching)
        over_budget["budget"]["requested"][field] = 101
        yield pytest.param(over_budget, False, "BUDGET_EXCEEDED", id=f"{field}-denied")

    l3 = deepcopy(matching)
    l3["tool"]["name"] = "start_model_cache_delete"
    l3["tool"]["risk_level"] = "L3"
    l3["plan"]["risk_level"] = "L3"
    yield pytest.param(l3, False, "L3_FORBIDDEN", id="l3-denied")


def _evaluate(policy_input: dict[str, object]) -> dict[str, object]:
    if OPA_PATH is None:
        pytest.skip(OPA_REQUIRED)
    completed = subprocess.run(  # noqa: S603
        [
            OPA_PATH,
            "eval",
            "--format=json",
            "--data",
            str(POLICY_PATH),
            "--stdin-input",
            "data.autopilot.authz.decision",
        ],
        input=json.dumps(policy_input),
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    return payload["result"][0]["expressions"][0]["value"]


@pytest.mark.parametrize(("policy_input", "allow", "reason_code"), tuple(_cases()))
def test_opa_policy_golden(
    policy_input: dict[str, object],
    allow: bool,
    reason_code: str,
) -> None:
    decision = _evaluate(policy_input)

    assert decision["allow"] is allow
    assert decision["reason_code"] == reason_code
