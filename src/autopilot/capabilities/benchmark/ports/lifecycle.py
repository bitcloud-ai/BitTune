"""Typed EvalScope lifecycle and Artifact binding contracts."""

from typing import Annotated, Literal, Self

from pydantic import StringConstraints, model_validator

from autopilot.capabilities.benchmark.domain.enums import BenchmarkProviderState
from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.base import NonEmptyStr, StrictModel
from autopilot.domain.identifiers import (
    BenchmarkRunId,
    JobId,
    PlanHash,
    PlanId,
    Sha256Digest,
)

ArtifactStorageKey = Annotated[
    str,
    StringConstraints(
        pattern=r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+){0,15}$",
        max_length=512,
    ),
]
MISMATCHED_RESOURCE_IDS = "benchmark and Job IDs must share the same stable suffix"


class BenchmarkAdapterCapabilities(StrictModel):
    schema_version: Literal["benchmark-adapter-capabilities/v1"] = (
        "benchmark-adapter-capabilities/v1"
    )
    provider: Literal["evalscope"] = "evalscope"
    api: Literal["python"] = "python"
    supports_baseline: Literal[True] = True
    supports_closed_loop: Literal[True] = True
    supports_open_loop: Literal[True] = True
    supports_sla_search: Literal[True] = True
    supports_cancel: Literal[True] = True


class RunnerArtifactLocation(StrictModel):
    """Root-confined internal location resolved from an Artifact ID."""

    schema_version: Literal["runner-artifact-location/v1"] = "runner-artifact-location/v1"
    root: Literal["output"] = "output"
    storage_key: ArtifactStorageKey


class BenchmarkStartContext(StrictModel):
    schema_version: Literal["benchmark-start-context/v1"] = "benchmark-start-context/v1"
    benchmark_run_id: BenchmarkRunId
    job_id: JobId
    plan_id: PlanId
    plan_hash: PlanHash
    idempotency_key: Sha256Digest
    request_id: NonEmptyStr
    compiled_spec_artifact: ArtifactRef

    @model_validator(mode="after")
    def validate_resource_suffix(self) -> Self:
        benchmark_suffix = self.benchmark_run_id.root.removeprefix("benchmark_")
        job_suffix = self.job_id.root.removeprefix("job_")
        if benchmark_suffix != job_suffix:
            raise ValueError(MISMATCHED_RESOURCE_IDS)
        return self


class BenchmarkOperation(StrictModel):
    schema_version: Literal["benchmark-operation/v1"] = "benchmark-operation/v1"
    benchmark_run_id: BenchmarkRunId
    job_id: JobId
    state: BenchmarkProviderState
    provider_resource_id: NonEmptyStr
    idempotent_replay: bool = False
    detail: NonEmptyStr | None = None
