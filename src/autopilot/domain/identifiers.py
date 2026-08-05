"""Stable identifiers and immutable content digests."""

from __future__ import annotations

import re
from typing import ClassVar, Self
from uuid import uuid4

from pydantic import ConfigDict, RootModel, model_validator

INVALID_STABLE_ID = "identifier must use its resource prefix and 32 lowercase hex characters"
INVALID_SHA256 = "digest must use sha256 followed by 64 lowercase hex characters"
INVALID_IMAGE_DIGEST = "image must be an immutable repository-at-sha256 reference"
INVALID_MODEL_REVISION = "model revision must be a 40- or 64-character lowercase commit hash"
INVALID_TOOL_NAME = "tool name does not use an allowed domain action form"
INVALID_REFERENCE_NAME = "secret reference must be a 3-64 character logical kebab-case name"


class StableId(RootModel[str]):
    """Typed UUID-based resource identifier with a fixed domain prefix."""

    model_config = ConfigDict(frozen=True)
    prefix: ClassVar[str]

    @model_validator(mode="after")
    def validate_format(self) -> Self:
        expected = rf"{re.escape(self.prefix)}_[0-9a-f]{{32}}"
        if re.fullmatch(expected, self.root) is None:
            raise ValueError(INVALID_STABLE_ID)
        return self

    @classmethod
    def new(cls) -> Self:
        """Create a new stable identifier of this resource type."""
        return cls(root=f"{cls.prefix}_{uuid4().hex}")

    def __str__(self) -> str:
        return self.root


class ExperimentId(StableId):
    prefix = "exp"


class PlanId(StableId):
    prefix = "plan"


class JobId(StableId):
    prefix = "job"


class ArtifactId(StableId):
    prefix = "artifact"


class HardwarePassportId(StableId):
    prefix = "env"


class ModelProfileId(StableId):
    prefix = "model"


class CandidateId(StableId):
    prefix = "cand"


class DeploymentId(StableId):
    prefix = "deployment"


class BenchmarkRunId(StableId):
    prefix = "benchmark"


class StudyId(StableId):
    prefix = "study"


class TrialId(StableId):
    prefix = "trial"


class ApprovalId(StableId):
    prefix = "approval"


class UserId(StableId):
    prefix = "user"


class ToolSetId(StableId):
    prefix = "toolset"


class Sha256Digest(RootModel[str]):
    """Lowercase SHA-256 digest with an explicit algorithm prefix."""

    model_config = ConfigDict(frozen=True)

    @model_validator(mode="after")
    def validate_format(self) -> Self:
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.root) is None:
            raise ValueError(INVALID_SHA256)
        return self

    def __str__(self) -> str:
        return self.root


class PlanHash(Sha256Digest):
    """RFC 8785 canonical execution-specification digest."""


class ImageDigest(RootModel[str]):
    """Immutable OCI image reference containing a SHA-256 digest."""

    model_config = ConfigDict(frozen=True)

    @model_validator(mode="after")
    def validate_format(self) -> Self:
        pattern = r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}"
        if re.fullmatch(pattern, self.root) is None:
            raise ValueError(INVALID_IMAGE_DIGEST)
        return self

    def __str__(self) -> str:
        return self.root


class ModelRevision(RootModel[str]):
    """Immutable model repository commit revision."""

    model_config = ConfigDict(frozen=True)

    @model_validator(mode="after")
    def validate_format(self) -> Self:
        if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", self.root) is None:
            raise ValueError(INVALID_MODEL_REVISION)
        return self

    def __str__(self) -> str:
        return self.root


class ToolName(RootModel[str]):
    """Agent-visible tool name restricted to the public action vocabulary."""

    model_config = ConfigDict(frozen=True)

    @model_validator(mode="after")
    def validate_format(self) -> Self:
        pattern = (
            r"(?:create_[a-z0-9_]+_plan|preview_[a-z0-9_]+|start_[a-z0-9_]+|"
            r"get_[a-z0-9_]+_(?:status|result)|cancel_[a-z0-9_]+)"
        )
        if re.fullmatch(pattern, self.root) is None:
            raise ValueError(INVALID_TOOL_NAME)
        return self

    def __str__(self) -> str:
        return self.root


class SecretRef(RootModel[str]):
    """Logical secret name that never contains a path or credential value."""

    model_config = ConfigDict(frozen=True)

    @model_validator(mode="after")
    def validate_format(self) -> Self:
        if re.fullmatch(r"[a-z][a-z0-9-]{2,63}", self.root) is None:
            raise ValueError(INVALID_REFERENCE_NAME)
        return self

    def __str__(self) -> str:
        return self.root
