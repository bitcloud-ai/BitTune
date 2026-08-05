"""Immutable model references and normalized model profiles."""

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.base import NonEmptyStr, StrictModel, UtcDatetime
from autopilot.domain.identifiers import ModelProfileId, ModelRevision
from autopilot.domain.provenance import Provenance
from autopilot.domain.workloads import RepositoryId

ArchitectureName = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{1,127}$"),
]
QuantizationName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$"),
]
INVALID_KV_HEADS = "KV head count cannot exceed attention head count"


class HuggingFaceModelRef(StrictModel):
    type: Literal["huggingface"] = "huggingface"
    repository_id: RepositoryId
    revision: ModelRevision


class ArtifactModelRef(StrictModel):
    type: Literal["artifact"] = "artifact"
    artifact: ArtifactRef
    revision: ModelRevision


ModelRef = Annotated[HuggingFaceModelRef | ArtifactModelRef, Field(discriminator="type")]


class ModelProfile(StrictModel):
    schema_version: Literal["model-profile/v1"] = "model-profile/v1"
    model_profile_id: ModelProfileId
    model_ref: ModelRef
    architectures: tuple[ArchitectureName, ...] = Field(min_length=1, max_length=8)
    parameter_count: int = Field(ge=1, le=10_000_000_000_000)
    layer_count: int = Field(ge=1, le=10_000)
    hidden_size: int = Field(ge=1, le=10_000_000)
    attention_heads: int = Field(ge=1, le=1_000_000)
    kv_heads: int = Field(ge=1, le=1_000_000)
    max_context_tokens: int = Field(ge=1, le=50_000_000)
    quantization: QuantizationName | None
    license_id: NonEmptyStr | None
    config_artifact: ArtifactRef
    provenance: Provenance
    captured_at: UtcDatetime

    @model_validator(mode="after")
    def validate_attention_shape(self) -> Self:
        if self.kv_heads > self.attention_heads:
            raise ValueError(INVALID_KV_HEADS)
        return self
