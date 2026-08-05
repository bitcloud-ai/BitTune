"""Authenticated human and internal service identity contracts."""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from autopilot.domain.base import StrictModel
from autopilot.domain.enums import UserRole
from autopilot.domain.identifiers import Sha256Digest, UserId

ServiceName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^[a-z][a-z0-9-]{2,63}$",
    ),
]


class SubjectKind(StrEnum):
    """Trust-boundary kind for authenticated actors."""

    HUMAN = "human"
    SERVICE = "service"


class BearerTokenHash(Sha256Digest):
    """SHA-256 digest of an opaque Bearer Token; never the plaintext Token."""


class HumanSubject(StrictModel):
    kind: Literal[SubjectKind.HUMAN] = SubjectKind.HUMAN
    user_id: UserId
    role: UserRole


class ServiceSubject(StrictModel):
    kind: Literal[SubjectKind.SERVICE] = SubjectKind.SERVICE
    service_name: ServiceName


type Subject = Annotated[HumanSubject | ServiceSubject, Field(discriminator="kind")]


class BearerTokenBinding(StrictModel):
    """Persistable Token-hash-to-human mapping loaded by the authentication boundary."""

    schema_version: Literal["bearer-token-binding/v1"] = "bearer-token-binding/v1"
    token_hash: BearerTokenHash
    subject: HumanSubject
