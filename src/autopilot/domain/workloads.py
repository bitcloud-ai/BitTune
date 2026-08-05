"""Provider-independent model and benchmark workload contracts."""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.base import StrictModel
from autopilot.domain.identifiers import ModelRevision

DatasetId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{2,63}-v[1-9][0-9]*$"),
]
RepositoryId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"),
]


class SyntheticFixedDataset(StrictModel):
    type: Literal["synthetic_fixed"] = "synthetic_fixed"
    dataset_id: DatasetId


class ArtifactDataset(StrictModel):
    type: Literal["artifact"] = "artifact"
    artifact: ArtifactRef


DatasetSpec = Annotated[SyntheticFixedDataset | ArtifactDataset, Field(discriminator="type")]


class TokenizerRef(StrictModel):
    repository_id: RepositoryId
    revision: ModelRevision


class SamplingSpec(StrictModel):
    temperature: Literal[0] = 0
    seed: int = Field(ge=0, le=4_294_967_295)


class WorkloadSpec(StrictModel):
    schema_version: Literal["workload/v1"] = "workload/v1"
    dataset: DatasetSpec
    tokenizer: TokenizerRef
    prompt_tokens: int = Field(ge=1, le=50_000_000)
    output_tokens: int = Field(ge=1, le=50_000_000)
    stream: bool
    ignore_eos: bool
    sampling: SamplingSpec
