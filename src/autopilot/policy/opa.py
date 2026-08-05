"""Fail-closed HTTP adapter for the fixed OPA authorization decision endpoint."""

from __future__ import annotations

import json
from typing import Final

import httpx
from pydantic import ValidationError

from autopilot.domain.base import NonEmptyStr, StrictModel
from autopilot.policy.models import PolicyDecision, PolicyInput, PolicyResult
from autopilot.policy.ports import PolicyClient

OPA_DECISION_PATH: Final = "/v1/data/autopilot/authz/decision"
OPA_UNAVAILABLE: Final = "OPA authorization service is unavailable"
OPA_INVALID_RESPONSE: Final = "OPA returned an invalid authorization response"


class PolicyUnavailableError(RuntimeError):
    """OPA could not produce a decision; callers must deny the action."""


class PolicyResponseError(RuntimeError):
    """OPA returned a response that cannot be trusted as an authorization decision."""


class _OpaResponse(StrictModel):
    decision_id: NonEmptyStr
    result: PolicyResult


class OpaPolicyClient(PolicyClient):
    """Call one fixed OPA document without forwarding credentials or arbitrary paths."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def evaluate(self, policy_input: PolicyInput) -> PolicyDecision:
        try:
            response = self._client.post(
                OPA_DECISION_PATH,
                json={"input": policy_input.model_dump(mode="json")},
            )
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise PolicyUnavailableError(OPA_UNAVAILABLE) from error
        if not response.is_success:
            raise PolicyUnavailableError(OPA_UNAVAILABLE)
        try:
            envelope = _OpaResponse.model_validate(response.json())
        except (json.JSONDecodeError, ValidationError) as error:
            raise PolicyResponseError(OPA_INVALID_RESPONSE) from error
        return PolicyDecision(
            decision_id=envelope.decision_id,
            allow=envelope.result.allow,
            reason_code=envelope.result.reason_code,
            requirements=envelope.result.requirements,
        )
