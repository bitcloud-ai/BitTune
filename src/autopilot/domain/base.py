"""Strict primitives shared by all persistent domain contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, StringConstraints


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_as_utc)]
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)]
SchemaVersion = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9-]*/v[1-9][0-9]*$", max_length=64),
]


class StrictModel(BaseModel):
    """Immutable Pydantic model that rejects unknown input fields."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        populate_by_name=True,
        str_strip_whitespace=True,
        validate_default=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)
