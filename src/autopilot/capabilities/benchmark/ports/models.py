"""Typed raw-report boundary implemented by the pinned EvalScope adapter."""

from typing import Literal, Self

from pydantic import Field, model_validator

from autopilot.capabilities.benchmark.domain.models import ProviderFieldName
from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.base import NonEmptyStr, StrictModel
from autopilot.domain.enums import TrafficMode
from autopilot.domain.identifiers import Sha256Digest

DUPLICATE_RAW_METRIC = "raw EvalScope metric names must be unique"
INVALID_RAW_ARTIFACT_PRODUCER = "raw EvalScope artifact must be produced by the bound adapter"


class RawMetricSample(StrictModel):
    name: ProviderFieldName
    value: float


class EvalScopeRawReport(StrictModel):
    schema_version: Literal["evalscope-raw-report/v1"] = "evalscope-raw-report/v1"
    provider_version: NonEmptyStr
    adapter_version: NonEmptyStr
    provider_profile_version: NonEmptyStr
    provider_profile_hash: Sha256Digest
    compiled_benchmark_hash: Sha256Digest
    traffic_mode: TrafficMode
    metrics: tuple[RawMetricSample, ...] = Field(min_length=1, max_length=256)
    oom: bool
    raw_artifact: ArtifactRef

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        names = [sample.name for sample in self.metrics]
        if len(names) != len(set(names)):
            raise ValueError(DUPLICATE_RAW_METRIC)
        if (
            self.raw_artifact.producer.component != "evalscope-adapter"
            or self.raw_artifact.producer.version != self.adapter_version
        ):
            raise ValueError(INVALID_RAW_ARTIFACT_PRODUCER)
        return self
