"""Typed evidence capability failures."""

from autopilot.capabilities.evidence.domain.enums import (
    ChampionPolicyCode,
    EvidenceValidationCode,
)


class ChampionPolicyError(ValueError):
    """A deterministic verification or selection failure."""

    def __init__(self, code: ChampionPolicyCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class EvidenceProviderError(RuntimeError):
    """A classified MLflow boundary failure without Provider stack details."""

    def __init__(
        self,
        code: EvidenceValidationCode,
        message: str,
        *,
        retryable: bool,
    ) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)
