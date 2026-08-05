"""Artifact references that never expose storage paths."""

from typing import Literal

from pydantic import Field

from autopilot.domain.base import NonEmptyStr, StrictModel
from autopilot.domain.identifiers import ArtifactId, Sha256Digest


class ArtifactProducer(StrictModel):
    component: NonEmptyStr
    version: NonEmptyStr


class ArtifactRef(StrictModel):
    schema_version: Literal["artifact-ref/v1"] = "artifact-ref/v1"
    artifact_id: ArtifactId
    sha256: Sha256Digest
    content_type: NonEmptyStr
    size_bytes: int = Field(ge=0, le=100_000_000_000)
    producer: ArtifactProducer
