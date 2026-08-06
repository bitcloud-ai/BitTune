"""Typed benchmark failures safe for application boundaries."""

from autopilot.capabilities.benchmark.domain.enums import BenchmarkValidationCode


class BenchmarkValidationError(ValueError):
    """A deterministic benchmark plan or normalization failure."""

    def __init__(self, code: BenchmarkValidationCode, field: str, message: str) -> None:
        self.code = code
        self.field = field
        super().__init__(message)


class BenchmarkProviderError(RuntimeError):
    """A classified EvalScope/Runner boundary failure."""

    def __init__(
        self,
        code: BenchmarkValidationCode,
        message: str,
        *,
        retryable: bool,
    ) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)
