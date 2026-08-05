"""Internal Artifact metadata shared by storage and persistence adapters."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Annotated, Final, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from autopilot.domain.artifacts import ArtifactProducer, ArtifactRef
from autopilot.domain.base import NonEmptyStr, StrictModel, UtcDatetime
from autopilot.domain.identifiers import ArtifactId, ExperimentId, Sha256Digest

ARTIFACT_MAX_BYTES: Final = 100_000_000_000
ARTIFACT_PAYLOAD_FILENAME: Final = "payload"

_CATEGORY_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_WINDOWS_RESERVED_NAMES = frozenset(
    {"aux", "con", "nul", "prn"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
_INVALID_CATEGORY = "artifact category must be a safe lowercase logical name"
_INVALID_STORAGE_BINDING = "artifact storage path does not match its logical identity"

ArtifactStoragePath = Annotated[str, StringConstraints(min_length=1, max_length=512)]


def validate_artifact_category(category: object) -> str:
    """Return a safe logical category or reject path-like and reserved values."""
    if type(category) is not str or _CATEGORY_PATTERN.fullmatch(category) is None:
        raise ValueError(_INVALID_CATEGORY)
    if category in _WINDOWS_RESERVED_NAMES:
        raise ValueError(_INVALID_CATEGORY)
    return category


def artifact_storage_path(
    experiment_id: ExperimentId,
    category: str,
    artifact_id: ArtifactId,
) -> str:
    """Build the only persisted relative path shape accepted by the MVP."""
    return PurePosixPath(
        "experiments",
        str(experiment_id),
        category,
        str(artifact_id),
        ARTIFACT_PAYLOAD_FILENAME,
    ).as_posix()


class ArtifactMetadata(StrictModel):
    """Internal persisted metadata; Agent-facing results use :class:`ArtifactRef`."""

    schema_version: Literal["artifact-metadata/v1"] = "artifact-metadata/v1"
    artifact_id: ArtifactId
    experiment_id: ExperimentId
    category: str
    content_type: NonEmptyStr
    size_bytes: int = Field(ge=0, le=ARTIFACT_MAX_BYTES)
    sha256: Sha256Digest
    created_at: UtcDatetime
    producer: ArtifactProducer
    storage_path: ArtifactStoragePath

    @field_validator("category")
    @classmethod
    def validate_category(cls, category: str) -> str:
        return validate_artifact_category(category)

    @model_validator(mode="after")
    def validate_storage_binding(self) -> Self:
        expected = artifact_storage_path(self.experiment_id, self.category, self.artifact_id)
        if self.storage_path != expected:
            raise ValueError(_INVALID_STORAGE_BINDING)
        return self

    @property
    def uri(self) -> str:
        """Return the stable URI without exposing a filesystem path."""
        return f"artifact://experiments/{self.experiment_id}/{self.category}/{self.artifact_id}"

    def to_ref(self) -> ArtifactRef:
        """Project internal storage metadata to the Agent-safe domain reference."""
        return ArtifactRef(
            artifact_id=self.artifact_id,
            sha256=self.sha256,
            content_type=self.content_type,
            size_bytes=self.size_bytes,
            producer=self.producer,
        )
