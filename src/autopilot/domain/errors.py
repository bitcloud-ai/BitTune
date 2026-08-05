"""Provider-safe typed error envelope."""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.base import LongText, NonEmptyStr, StrictModel
from autopilot.domain.enums import ErrorCategory, SuggestedAction

ErrorCode = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{2,127}$")]
FieldPath = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_.\[\]-]{1,256}$")]


class FieldError(StrictModel):
    path: FieldPath
    reason: NonEmptyStr


class DomainError(StrictModel):
    code: ErrorCode
    category: ErrorCategory
    message: LongText
    field_errors: tuple[FieldError, ...] = Field(default=(), max_length=64)
    retryable: bool
    provider: NonEmptyStr | None = None
    provider_detail_ref: ArtifactRef | None = None
    suggested_actions: tuple[SuggestedAction, ...] = Field(default=(), max_length=8)


class ErrorEnvelope(StrictModel):
    schema_version: Literal["error-envelope/v1"] = "error-envelope/v1"
    error: DomainError
