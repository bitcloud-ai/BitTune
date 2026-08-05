"""Application-facing authorization policy Port."""

from typing import Protocol

from autopilot.policy.models import PolicyDecision, PolicyInput


class PolicyClient(Protocol):
    def evaluate(self, policy_input: PolicyInput) -> PolicyDecision: ...
