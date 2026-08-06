"""Typed capacity planning failures."""

from autopilot.capabilities.capacity.domain.enums import CapacityValidationCode


class CapacityValidationError(ValueError):
    """A deterministic planning input or provider-output failure."""

    def __init__(self, code: CapacityValidationCode, message: str, field: str = "") -> None:
        self.code = code
        self.field = field
        super().__init__(message)


class CapacityProviderUnavailableError(RuntimeError):
    """The fixed llm-d Planner profile cannot be executed."""

    def __init__(
        self,
        message: str = "the verified llm-d Planner Runner client is not configured",
    ) -> None:
        self.code = CapacityValidationCode.PROVIDER_UNAVAILABLE
        super().__init__(message)
