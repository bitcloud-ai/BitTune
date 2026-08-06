"""Typed deployment validation failures safe for application boundaries."""

from autopilot.capabilities.deployment.domain.enums import DeploymentValidationCode


class DeploymentValidationError(ValueError):
    """A deterministic deployment specification validation failure."""

    def __init__(self, code: DeploymentValidationCode, field: str, message: str) -> None:
        self.code = code
        self.field = field
        super().__init__(message)


class DeploymentProviderError(RuntimeError):
    """A classified vLLM/Runner boundary failure with no provider stack trace."""

    def __init__(
        self,
        code: DeploymentValidationCode,
        message: str,
        *,
        retryable: bool,
    ) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)
