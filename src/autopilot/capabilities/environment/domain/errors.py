"""Typed, provider-safe environment errors."""

from autopilot.capabilities.environment.domain.enums import EnvironmentValidationCode


class EnvironmentValidationError(ValueError):
    """A deterministic environment validation or compatibility failure."""

    def __init__(self, code: EnvironmentValidationCode, message: str, field: str = "") -> None:
        self.code = code
        self.field = field
        super().__init__(message)


class EnvironmentProviderUnavailableError(RuntimeError):
    """Raised when an external environment provider is not installed or verified."""

    def __init__(
        self,
        message: str,
        code: EnvironmentValidationCode = EnvironmentValidationCode.PROVIDER_UNAVAILABLE,
    ) -> None:
        self.code = code
        super().__init__(message)
