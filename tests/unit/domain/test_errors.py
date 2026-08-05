import pytest
from pydantic import ValidationError

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


@pytest.mark.parametrize(
    "category",
    [
        ErrorCategory.VALIDATION_ERROR,
        ErrorCategory.POLICY_DENIED,
        ErrorCategory.MODEL_INCOMPATIBLE,
        ErrorCategory.OOM,
        ErrorCategory.QUALITY_GATE_FAILED,
    ],
)
def test_non_retryable_categories_reject_retryable_true(category: ErrorCategory) -> None:
    with pytest.raises(ValidationError, match="not retryable"):
        DomainError(
            code="NON_RETRYABLE",
            category=category,
            message="input must change before retry",
            retryable=True,
        )
