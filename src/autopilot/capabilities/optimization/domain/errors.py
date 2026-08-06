"""Typed optimization failures safe for application boundaries."""

from autopilot.capabilities.optimization.domain.enums import (
    OptimizationProviderCode,
    OptimizationValidationCode,
    TrialExecutionCode,
    TrialExecutionStage,
)


class OptimizationValidationError(ValueError):
    """A deterministic search-space or feasibility failure."""

    def __init__(self, code: OptimizationValidationCode, field: str, message: str) -> None:
        self.code = code
        self.field = field
        super().__init__(message)


class TrialExecutionError(RuntimeError):
    """Fail-closed orchestration error after a Provider boundary was reached."""

    def __init__(
        self,
        code: TrialExecutionCode,
        message: str,
        *,
        retryable: bool,
    ) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class TrialExecutionPendingError(RuntimeError):
    """Signal that an asynchronous Provider job remains non-terminal."""

    def __init__(self, stage: TrialExecutionStage, provider_resource_id: str) -> None:
        self.stage = stage
        self.provider_resource_id = provider_resource_id
        super().__init__(f"{stage.value} Provider job is still running")


class OptimizationProviderError(RuntimeError):
    """Redacted failure from the pinned Optimization Provider boundary."""

    def __init__(
        self,
        code: OptimizationProviderCode,
        message: str,
        *,
        retryable: bool,
    ) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class OptimizationTrialNotFoundError(RuntimeError):
    """A persisted Trial key does not exist."""


class OptimizationTrialConflictError(RuntimeError):
    """A persisted Trial cannot make the requested transition."""
