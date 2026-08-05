"""Typed provenance for estimated, measured, and derived facts."""

from typing import Annotated, Literal

from pydantic import Field

from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.base import NonEmptyStr, StrictModel
from autopilot.domain.enums import Confidence, MeasurementSource


class EstimatedProvenance(StrictModel):
    source: Literal[MeasurementSource.ESTIMATED] = MeasurementSource.ESTIMATED
    provider: NonEmptyStr
    provider_version: NonEmptyStr
    adapter_version: NonEmptyStr
    confidence: Confidence
    calculation_artifact: ArtifactRef


class MeasuredProvenance(StrictModel):
    source: Literal[MeasurementSource.MEASURED] = MeasurementSource.MEASURED
    provider: NonEmptyStr
    provider_version: NonEmptyStr
    adapter_version: NonEmptyStr
    raw_artifact: ArtifactRef


class DerivedProvenance(StrictModel):
    source: Literal[MeasurementSource.DERIVED] = MeasurementSource.DERIVED
    provider: NonEmptyStr
    provider_version: NonEmptyStr
    adapter_version: NonEmptyStr
    calculation_artifact: ArtifactRef
    input_artifacts: tuple[ArtifactRef, ...] = Field(min_length=1, max_length=64)


Provenance = Annotated[
    EstimatedProvenance | MeasuredProvenance | DerivedProvenance,
    Field(discriminator="source"),
]
