"""Strict primitives shared by all persistent domain contracts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum
from types import UnionType
from typing import Annotated, Literal, Union, get_args, get_origin

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    StringConstraints,
    model_validator,
)


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_as_utc)]
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)]
SchemaVersion = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9-]*/v[1-9][0-9]*$", max_length=64),
]
MAPPING_TYPE_ARGUMENT_COUNT = 2


def _literal_matches(value: object, expected: object) -> bool:
    if isinstance(expected, Enum):
        expected = expected.value
    return type(value) is type(expected) and value == expected


def _union_coercion_error(options: tuple[object, ...], value: object, path: str) -> str | None:
    errors = [_scalar_coercion_error(option, value, path) for option in options]
    return None if None in errors else errors[0]


def _sequence_coercion_error(arguments: tuple[object, ...], value: object, path: str) -> str | None:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return None
    item_annotations = arguments[:-1] if arguments and arguments[-1] is not Ellipsis else ()
    if arguments and arguments[-1] is Ellipsis:
        item_annotations = (arguments[0],) * len(value)
    if len(item_annotations) == 1 and len(value) != 1:
        item_annotations = item_annotations * len(value)
    for index, (item_annotation, item) in enumerate(zip(item_annotations, value, strict=False)):
        error = _scalar_coercion_error(item_annotation, item, f"{path}[{index}]")
        if error is not None:
            return error
    return None


def _mapping_coercion_error(arguments: tuple[object, ...], value: object, path: str) -> str | None:
    if not isinstance(value, Mapping) or len(arguments) != MAPPING_TYPE_ARGUMENT_COUNT:
        return None
    for key, item in value.items():
        key_error = _scalar_coercion_error(arguments[0], key, f"{path}.<key>")
        if key_error is not None:
            return key_error
        item_error = _scalar_coercion_error(arguments[1], item, f"{path}.{key}")
        if item_error is not None:
            return item_error
    return None


def _primitive_coercion_error(annotation: object, value: object, path: str) -> str | None:
    if annotation is bool and type(value) is not bool:
        return f"{path} must be a boolean without coercion"
    if annotation is int and type(value) is not int:
        return f"{path} must be an integer without coercion"
    if annotation is float and type(value) not in {int, float}:
        return f"{path} must be a number without coercion"
    if annotation is str and type(value) is not str:
        return f"{path} must be a string without coercion"
    return None


def _scalar_coercion_error(annotation: object, value: object, path: str) -> str | None:
    """Return an error for unsafe scalar coercion while preserving JSON containers."""
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Annotated:
        return _scalar_coercion_error(arguments[0], value, path)
    if origin in {Union, UnionType}:
        return _union_coercion_error(arguments, value, path)
    if origin is Literal:
        matches = any(_literal_matches(value, expected) for expected in arguments)
        return None if matches else f"{path} does not match the exact literal type"
    if origin in {tuple, list, set, frozenset}:
        return _sequence_coercion_error(arguments, value, path)
    if origin is dict:
        return _mapping_coercion_error(arguments, value, path)
    return _primitive_coercion_error(annotation, value, path)


class StrictModel(BaseModel):
    """Immutable Pydantic model that rejects unknown input fields."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        str_strip_whitespace=True,
        validate_default=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )

    @model_validator(mode="before")
    @classmethod
    def reject_unsafe_scalar_coercion(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        for name, field in cls.model_fields.items():
            input_name = field.alias if isinstance(field.alias, str) else name
            if input_name not in value:
                continue
            error = _scalar_coercion_error(field.annotation, value[input_name], name)
            if error is not None:
                raise ValueError(error)
        return value


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)
