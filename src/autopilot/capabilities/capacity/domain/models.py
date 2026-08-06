"""Versioned llm-d Planner anti-corruption contracts."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.base import NonEmptyStr, StrictModel
from autopilot.domain.candidates import DeploymentCandidate
from autopilot.domain.enums import Confidence, MeasurementSource
from autopilot.domain.hardware import HardwarePassport
from autopilot.domain.hashing import compute_content_hash
from autopilot.domain.identifiers import ImageDigest, PlanHash, Sha256Digest
from autopilot.domain.models import ModelProfile, ModelRef
from autopilot.domain.plans import ExecutionSpecification
from autopilot.domain.provenance import EstimatedProvenance
from autopilot.domain.workloads import WorkloadSpec

INVALID_MEMORY_TOTAL = "capacity memory components must sum to total_required_bytes"
INVALID_FIT = "capacity fit and generated candidates are inconsistent"
INVALID_CANDIDATES = "fit capacity plans require the three fixed candidate profiles"
INVALID_PLAN_HASH = "capacity plan hash does not match its immutable material"
INVALID_ESTIMATE_PROVENANCE = "capacity estimate provenance does not match its provider binding"
INVALID_EXECUTION_PROFILE = "capacity execution profile must match planning specification"


class CapacityPlannerVersionProfile(StrictModel):
    """One Phase-0 verified llm-d Capacity Planner container profile."""

    schema_version: Literal["capacity-planner-version-profile/v1"] = (
        "capacity-planner-version-profile/v1"
    )
    profile_version: NonEmptyStr
    provider_version: NonEmptyStr
    adapter_version: NonEmptyStr
    planner_image: ImageDigest
    rtx_5090_verified: Literal[True]
    candidate_engine_version: NonEmptyStr
    candidate_engine_image: ImageDigest
    deployment_adapter_version: NonEmptyStr
    memory_safety_margin_ratio: float = Field(ge=0.05, le=0.30)


class CapacityPlanningSpecification(StrictModel):
    """Provider-independent immutable input for a single-GPU capacity plan."""

    schema_version: Literal["capacity-planning-specification/v1"] = (
        "capacity-planning-specification/v1"
    )
    provider: Literal["llm-d-planner"] = "llm-d-planner"
    provider_version: NonEmptyStr
    adapter_version: NonEmptyStr
    provider_profile_version: NonEmptyStr
    model_ref: ModelRef
    hardware_passport: HardwarePassport
    workload: WorkloadSpec
    requested_max_model_len: int = Field(ge=1, le=50_000_000)
    requested_gpu_memory_utilization: float = Field(ge=0.80, le=0.94)
    expected_concurrency: int = Field(ge=1, le=1_024)
    tensor_parallel_size: Literal[1] = 1
    trust_remote_code: Literal[False] = False


class CapacityExecutionSpecification(ExecutionSpecification):
    """Persisted Capacity Plan material with the Experiment budget binding."""

    schema_version: Literal["capacity-execution-specification/v1"] = (
        "capacity-execution-specification/v1"
    )
    provider: Literal["llm-d-planner"] = "llm-d-planner"
    planning: CapacityPlanningSpecification

    @model_validator(mode="after")
    def validate_provider_binding(self) -> Self:
        if (
            self.provider_version != self.planning.provider_version
            or self.adapter_version != self.planning.adapter_version
            or self.provider_profile_version != self.planning.provider_profile_version
        ):
            raise ValueError(INVALID_EXECUTION_PROFILE)
        return self


class CapacityMemoryEstimate(StrictModel):
    """Planner memory accounting in bytes."""

    weights_bytes: int = Field(ge=0, le=1_000_000_000_000)
    kv_cache_budget_bytes: int = Field(ge=0, le=1_000_000_000_000)
    activation_bytes: int = Field(ge=0, le=1_000_000_000_000)
    overhead_bytes: int = Field(ge=0, le=1_000_000_000_000)
    total_required_bytes: int = Field(ge=0, le=1_000_000_000_000)

    @model_validator(mode="after")
    def validate_total(self) -> Self:
        if (
            self.weights_bytes
            + self.kv_cache_budget_bytes
            + self.activation_bytes
            + self.overhead_bytes
            != self.total_required_bytes
        ):
            raise ValueError(INVALID_MEMORY_TOTAL)
        return self


class CapacityLimits(StrictModel):
    estimated_max_context_tokens: int = Field(ge=0, le=50_000_000)
    estimated_concurrency: int = Field(ge=0, le=1_024)


class PlannerRawEstimate(StrictModel):
    """Closed DTO returned by the llm-d anti-corruption adapter."""

    schema_version: Literal["planner-raw-estimate/v1"] = "planner-raw-estimate/v1"
    fit: bool
    model_profile: ModelProfile
    memory: CapacityMemoryEstimate
    limits: CapacityLimits
    calculation_artifact: ArtifactRef


class CapacityEstimate(StrictModel):
    """Normalized estimate, explicitly not a measurement."""

    schema_version: Literal["capacity-estimate/v1"] = "capacity-estimate/v1"
    fit: bool
    source: Literal[MeasurementSource.ESTIMATED] = MeasurementSource.ESTIMATED
    confidence: Confidence
    memory: CapacityMemoryEstimate
    limits: CapacityLimits
    requires_benchmark_validation: Literal[True] = True
    provenance: EstimatedProvenance

    @model_validator(mode="after")
    def validate_provenance(self) -> Self:
        if (
            self.source != self.provenance.source
            or self.confidence != self.provenance.confidence
            or self.provenance.provider != "llm-d-planner"
        ):
            raise ValueError(INVALID_ESTIMATE_PROVENANCE)
        return self


class CapacityPlanMaterial(StrictModel):
    schema_version: Literal["capacity-plan-material/v1"] = "capacity-plan-material/v1"
    provider_version: NonEmptyStr
    adapter_version: NonEmptyStr
    provider_profile_version: NonEmptyStr
    hardware_passport_hash: Sha256Digest
    model_profile: ModelProfile
    workload_hash: Sha256Digest
    estimate: CapacityEstimate
    candidates: tuple[DeploymentCandidate, ...] = Field(max_length=5)


class CapacityPlan(StrictModel):
    """Immutable capacity result with a canonical approval/audit hash."""

    schema_version: Literal["capacity-plan/v1"] = "capacity-plan/v1"
    provider_version: NonEmptyStr
    adapter_version: NonEmptyStr
    provider_profile_version: NonEmptyStr
    hardware_passport_hash: Sha256Digest
    model_profile: ModelProfile
    workload_hash: Sha256Digest
    estimate: CapacityEstimate
    candidates: tuple[DeploymentCandidate, ...] = Field(max_length=5)
    plan_hash: PlanHash

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        profiles = tuple(candidate.profile for candidate in self.candidates)
        if self.estimate.fit != bool(self.candidates):
            raise ValueError(INVALID_FIT)
        if self.estimate.fit and profiles != ("conservative", "balanced", "throughput"):
            raise ValueError(INVALID_CANDIDATES)
        material = CapacityPlanMaterial(
            provider_version=self.provider_version,
            adapter_version=self.adapter_version,
            provider_profile_version=self.provider_profile_version,
            hardware_passport_hash=self.hardware_passport_hash,
            model_profile=self.model_profile,
            workload_hash=self.workload_hash,
            estimate=self.estimate,
            candidates=self.candidates,
        )
        if self.plan_hash.root != compute_content_hash(material).root:
            raise ValueError(INVALID_PLAN_HASH)
        return self
