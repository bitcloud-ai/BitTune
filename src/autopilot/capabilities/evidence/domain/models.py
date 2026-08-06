"""Versioned evidence and deterministic Champion policy contracts."""

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from autopilot.capabilities.benchmark.domain.models import BenchmarkResult
from autopilot.capabilities.evidence.domain.enums import EvidenceProviderState
from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.base import NonEmptyStr, StrictModel, UtcDatetime
from autopilot.domain.candidates import DeploymentCandidate
from autopilot.domain.enums import TrialStatus
from autopilot.domain.identifiers import ExperimentId, PlanHash, PlanId, Sha256Digest, TrialId
from autopilot.domain.trials import ChampionSelection, TrialRecord

INVALID_TRIAL_CANDIDATE = "evidence Trial and Candidate identifiers do not match"
INVALID_EVIDENCE_LIFECYCLE = "evidence can only be recorded for a terminal Trial"
MISSING_MEASURED_RESULT = "measured Trial evidence requires a normalized BenchmarkResult"
INVALID_MEASURED_BINDING = "Trial provenance does not match its normalized benchmark result"
INVALID_EVIDENCE_TIME_RANGE = "evidence end time must not precede its start time"

CodeRevision = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$"),
]


class ChampionPolicy(StrictModel):
    schema_version: Literal["champion-policy/v1"] = "champion-policy/v1"
    top_candidate_count: Literal[3] = 3
    verification_repeats: int = Field(ge=2, le=20)
    max_coefficient_of_variation: float = Field(gt=0, le=1)
    noise_multiplier: float = Field(ge=1, le=5)
    minimum_relative_improvement: float = Field(gt=0, le=1)


class EvidenceVersionProfile(StrictModel):
    """A registered, fixed MLflow Tracking version binding."""

    schema_version: Literal["evidence-version-profile/v1"] = "evidence-version-profile/v1"
    profile_version: NonEmptyStr
    provider_version: NonEmptyStr
    adapter_version: NonEmptyStr
    contract_verified: Literal[True] = True


class EvidenceRunRequest(StrictModel):
    """Immutable, provider-independent material for one Tracking Run."""

    schema_version: Literal["evidence-run-request/v1"] = "evidence-run-request/v1"
    experiment_id: ExperimentId
    candidate: DeploymentCandidate
    trial: TrialRecord
    benchmark_result: BenchmarkResult | None = None
    hardware_passport_artifact: ArtifactRef | None = None
    model_profile_artifact: ArtifactRef | None = None
    requirements_artifact: ArtifactRef | None = None
    workload_artifact: ArtifactRef | None = None
    logs_artifact: ArtifactRef | None = None
    code_revision: CodeRevision
    idempotency_key: Sha256Digest
    started_at: UtcDatetime
    ended_at: UtcDatetime

    @model_validator(mode="after")
    def validate_bindings(self) -> Self:
        if self.trial.candidate_id != self.candidate.candidate_id:
            raise ValueError(INVALID_TRIAL_CANDIDATE)
        if self.ended_at < self.started_at:
            raise ValueError(INVALID_EVIDENCE_TIME_RANGE)
        if self.trial.status is TrialStatus.SUGGESTED:
            raise ValueError(INVALID_EVIDENCE_LIFECYCLE)
        measured_statuses = {TrialStatus.COMPLETED, TrialStatus.CONSTRAINT_FAILED}
        if self.trial.status in measured_statuses:
            result = self.benchmark_result
            provenance = self.trial.provenance
            if result is None or provenance is None:
                raise ValueError(MISSING_MEASURED_RESULT)
            if (
                provenance.provider != result.provider
                or provenance.provider_version != result.provider_version
                or provenance.adapter_version != result.adapter_version
                or provenance.raw_artifact.sha256 != result.raw_report_hash
            ):
                raise ValueError(INVALID_MEASURED_BINDING)
        return self


class EvidenceRunRef(StrictModel):
    """Stable reference to an external Tracking Run."""

    schema_version: Literal["evidence-run-ref/v1"] = "evidence-run-ref/v1"
    provider: Literal["mlflow"] = "mlflow"
    provider_version: NonEmptyStr
    adapter_version: NonEmptyStr
    provider_profile_version: NonEmptyStr
    provider_run_id: NonEmptyStr
    experiment_id: ExperimentId
    trial_id: TrialId
    request_hash: Sha256Digest
    idempotent_replay: bool = False


class EvidenceRunStatus(StrictModel):
    """Normalized Tracking Run state used during reconciliation."""

    schema_version: Literal["evidence-run-status/v1"] = "evidence-run-status/v1"
    run: EvidenceRunRef
    state: EvidenceProviderState
    started_at: UtcDatetime | None = None
    ended_at: UtcDatetime | None = None


class EvidenceBundleManifest(StrictModel):
    """Complete, replayable Artifact index for one verified optimization result."""

    schema_version: Literal["evidence-bundle-manifest/v1"] = "evidence-bundle-manifest/v1"
    experiment_id: ExperimentId
    plan_id: PlanId
    plan_hash: PlanHash
    hardware_passport: ArtifactRef
    model_profile: ArtifactRef
    model_revision: ArtifactRef
    engine_image_digest: ArtifactRef
    requirements: ArtifactRef
    workload: ArtifactRef
    slo: ArtifactRef
    candidate_artifacts: tuple[ArtifactRef, ...] = Field(min_length=1, max_length=480)
    trial_artifacts: tuple[ArtifactRef, ...] = Field(min_length=1, max_length=2_000)
    verification_artifacts: tuple[ArtifactRef, ...] = Field(min_length=1, max_length=64)
    champion_artifact: ArtifactRef
    report_artifact: ArtifactRef
    checksums_artifact: ArtifactRef
    code_revision: CodeRevision


class EvidenceBundle(StrictModel):
    """Agent-safe result containing only Artifact references and selection facts."""

    schema_version: Literal["evidence-bundle/v1"] = "evidence-bundle/v1"
    manifest: EvidenceBundleManifest
    manifest_artifact: ArtifactRef
    champion_selection: ChampionSelection
