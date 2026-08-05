from autopilot.domain.enums import ErrorCategory, SuggestedAction
from autopilot.domain.errors import DomainError, ErrorEnvelope, FieldError


def test_error_envelope_contains_typed_field_errors() -> None:
    envelope = ErrorEnvelope(
        error=DomainError(
            code="BENCHMARK_OPEN_LOOP_RATE_INVALID",
            category=ErrorCategory.VALIDATION_ERROR,
            message="request rate must be positive",
            field_errors=(FieldError(path="traffic.request_rates[0]", reason="must_be_positive"),),
            retryable=False,
            provider="evalscope",
            suggested_actions=(SuggestedAction.REVISE_PLAN,),
        )
    )

    assert envelope.error.retryable is False
    assert envelope.error.field_errors[0].path == "traffic.request_rates[0]"
