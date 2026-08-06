"""Closed vLLM search-space and pinned Optuna Study contracts for the MVP."""

from decimal import Decimal
from typing import Literal, Self

from pydantic import Field, model_validator

from autopilot.capabilities.evidence.domain.models import ChampionPolicy
from autopilot.capabilities.optimization.domain.enums import OptimizationProviderTrialState
from autopilot.domain.base import NonEmptyStr, StrictModel
from autopilot.domain.candidates import DeploymentCandidate, VllmTuningSpec
from autopilot.domain.constraints import ObjectiveSpec, SloSpec, validate_workload_against_slo
from autopilot.domain.enums import TrialStatus
from autopilot.domain.hashing import compute_content_hash
from autopilot.domain.identifiers import PlanHash, Sha256Digest, StudyId
from autopilot.domain.plans import ExecutionSpecification
from autopilot.domain.workloads import WorkloadSpec

MAX_GPU_MEMORY_GRID_POINTS = 15
INVALID_FLOAT_RANGE = (
    "search range must be increasing, exactly divisible by its step, and contain at most 15 values"
)
INVALID_CHOICES = "search-space categorical choices must be unique and ordered"
INVALID_COMPILED_CONFIGURATIONS = "compiled Optuna configurations must be unique"
INVALID_PROVIDER_TRIAL = "Optuna Trial state does not match its normalized result"
INVALID_TRIAL_OUTCOME = "Optuna tell outcome does not match the domain Trial status"
INVALID_OPTIMIZATION_BINDING = "Optimization Plan material has inconsistent immutable bindings"
INVALID_CONVERGENCE_POLICY = "convergence patience cannot exceed the Trial budget"
MVP_VERIFICATION_REPEATS = 2


class GpuMemoryUtilizationRange(StrictModel):
    low: float = Field(ge=0.80, le=0.94)
    high: float = Field(ge=0.80, le=0.94)
    step: float = Field(gt=0, le=0.14)

    @model_validator(mode="after")
    def validate_grid(self) -> Self:
        low = Decimal(str(self.low))
        high = Decimal(str(self.high))
        step = Decimal(str(self.step))
        distance = high - low
        if (
            high <= low
            or distance % step != 0
            or int(distance / step) + 1 > MAX_GPU_MEMORY_GRID_POINTS
        ):
            raise ValueError(INVALID_FLOAT_RANGE)
        return self


class VllmSearchSpaceSpec(StrictModel):
    schema_version: Literal["vllm-search-space/v1"] = "vllm-search-space/v1"
    profile_name: NonEmptyStr
    objective: ObjectiveSpec
    slo: SloSpec
    gpu_memory_utilization: GpuMemoryUtilizationRange
    max_num_seqs: tuple[Literal[4, 8, 16, 32], ...] = Field(min_length=1, max_length=4)
    max_num_batched_tokens: tuple[Literal[2048, 4096, 8192, 16384], ...] = Field(
        min_length=1, max_length=4
    )
    enable_chunked_prefill: tuple[bool, ...] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def validate_choices(self) -> Self:
        if (
            tuple(sorted(set(self.max_num_seqs))) != self.max_num_seqs
            or tuple(sorted(set(self.max_num_batched_tokens))) != self.max_num_batched_tokens
            or tuple(sorted(set(self.enable_chunked_prefill))) != self.enable_chunked_prefill
        ):
            raise ValueError(INVALID_CHOICES)
        return self


class OptunaVersionProfile(StrictModel):
    """A registered, fixed Optuna SDK and adapter binding."""

    schema_version: Literal["optuna-version-profile/v1"] = "optuna-version-profile/v1"
    profile_version: NonEmptyStr
    provider_version: NonEmptyStr
    adapter_version: NonEmptyStr
    sampler: Literal["TPESampler"] = "TPESampler"
    contract_verified: Literal[True] = True


class OptunaStudyDefinition(StrictModel):
    """Immutable domain material embedded in an Optimization Plan."""

    schema_version: Literal["optuna-study-definition/v1"] = "optuna-study-definition/v1"
    study_id: StudyId
    base_parameters: VllmTuningSpec
    search_space: VllmSearchSpaceSpec
    sampler_seed: int = Field(ge=0, le=4_294_967_295)


class OptimizationConvergencePolicy(StrictModel):
    """Fixed M7 stop policy evaluated only from persisted facts."""

    schema_version: Literal["optimization-convergence-policy/v1"] = (
        "optimization-convergence-policy/v1"
    )
    minimum_trials: Literal[10] = 10
    maximum_trials: int = Field(ge=10, le=20)
    no_improvement_trials: int = Field(ge=2, le=10)
    minimum_relative_improvement: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def validate_patience(self) -> Self:
        if self.no_improvement_trials > self.maximum_trials:
            raise ValueError(INVALID_CONVERGENCE_POLICY)
        return self


class OptimizationExecutionSpecification(ExecutionSpecification):
    """Immutable Optimization Plan executed only after L2 authorization."""

    schema_version: Literal["optimization-execution-specification/v1"] = (
        "optimization-execution-specification/v1"
    )
    provider: Literal["optuna"] = "optuna"
    definition: OptunaStudyDefinition
    base_candidate: DeploymentCandidate
    workload: WorkloadSpec
    convergence: OptimizationConvergencePolicy
    champion_policy: ChampionPolicy

    @model_validator(mode="after")
    def validate_bindings(self) -> Self:
        if (
            self.definition.base_parameters != self.base_candidate.parameters
            or compute_content_hash(self.workload) != self.base_candidate.workload_hash
            or self.champion_policy.verification_repeats != MVP_VERIFICATION_REPEATS
        ):
            raise ValueError(INVALID_OPTIMIZATION_BINDING)
        validate_workload_against_slo(self.workload, self.definition.search_space.slo)
        return self


class CompiledOptunaStudy(StrictModel):
    """Provider DTO produced only from an approved, immutable Plan."""

    schema_version: Literal["compiled-optuna-study/v1"] = "compiled-optuna-study/v1"
    provider: Literal["optuna"] = "optuna"
    provider_version: NonEmptyStr
    adapter_version: NonEmptyStr
    provider_profile_version: NonEmptyStr
    study_id: StudyId
    provider_study_name: NonEmptyStr
    plan_hash: PlanHash
    objective: ObjectiveSpec
    sampler: Literal["TPESampler"] = "TPESampler"
    sampler_seed: int = Field(ge=0, le=4_294_967_295)
    configurations: tuple[VllmTuningSpec, ...] = Field(min_length=1, max_length=480)

    @model_validator(mode="after")
    def validate_configurations(self) -> Self:
        serialized = tuple(item.model_dump_json() for item in self.configurations)
        if len(serialized) != len(set(serialized)):
            raise ValueError(INVALID_COMPILED_CONFIGURATIONS)
        return self


class OptimizationStudyRef(StrictModel):
    """Stable reference to a persisted Optuna Study."""

    schema_version: Literal["optimization-study-ref/v1"] = "optimization-study-ref/v1"
    provider: Literal["optuna"] = "optuna"
    provider_version: NonEmptyStr
    adapter_version: NonEmptyStr
    provider_profile_version: NonEmptyStr
    study_id: StudyId
    provider_study_name: NonEmptyStr
    study_material_hash: Sha256Digest


class OptimizationSuggestion(StrictModel):
    """One sampled member of the compiled, closed configuration set."""

    schema_version: Literal["optimization-suggestion/v1"] = "optimization-suggestion/v1"
    study_id: StudyId
    trial_number: int = Field(ge=0, le=1_000_000)
    configuration_index: int = Field(ge=0, lt=480)
    parameters: VllmTuningSpec


class OptimizationProviderTrial(StrictModel):
    """Reconciliation view of one Provider-owned Trial."""

    schema_version: Literal["optimization-provider-trial/v1"] = "optimization-provider-trial/v1"
    suggestion: OptimizationSuggestion
    state: OptimizationProviderTrialState
    objective_value: float | None = Field(default=None, ge=0)
    domain_status: TrialStatus | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.state is OptimizationProviderTrialState.RUNNING:
            if self.domain_status is TrialStatus.SUGGESTED or (
                (self.domain_status is TrialStatus.COMPLETED) != (self.objective_value is not None)
            ):
                raise ValueError(INVALID_PROVIDER_TRIAL)
        elif self.state is OptimizationProviderTrialState.COMPLETED:
            if self.objective_value is None or self.domain_status is not TrialStatus.COMPLETED:
                raise ValueError(INVALID_PROVIDER_TRIAL)
        elif self.objective_value is not None or self.domain_status in {
            TrialStatus.SUGGESTED,
            TrialStatus.COMPLETED,
        }:
            raise ValueError(INVALID_PROVIDER_TRIAL)
        return self


class OptimizationTrialOutcome(StrictModel):
    """Typed Controller result passed to Optuna tell."""

    schema_version: Literal["optimization-trial-outcome/v1"] = "optimization-trial-outcome/v1"
    trial_number: int = Field(ge=0, le=1_000_000)
    status: TrialStatus
    objective_value: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.status is TrialStatus.COMPLETED:
            if self.objective_value is None:
                raise ValueError(INVALID_TRIAL_OUTCOME)
        elif self.status is TrialStatus.SUGGESTED or self.objective_value is not None:
            raise ValueError(INVALID_TRIAL_OUTCOME)
        return self
