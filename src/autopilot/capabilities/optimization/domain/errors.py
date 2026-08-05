"""Typed optimization failures safe for application boundaries."""

from autopilot.capabilities.optimization.domain.enums import OptimizationValidationCode


class OptimizationValidationError(ValueError):
    """A deterministic search-space or feasibility failure."""

    def __init__(self, code: OptimizationValidationCode, field: str, message: str) -> None:
        self.code = code
        self.field = field
        super().__init__(message)
