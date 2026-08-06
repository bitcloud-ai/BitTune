"""Execute one approved Trial through existing deterministic capability ports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from autopilot.capabilities.benchmark.domain.enums import BenchmarkProviderState
from autopilot.capabilities.benchmark.domain.errors import (
    BenchmarkProviderError,
    BenchmarkValidationError,
)
from autopilot.capabilities.benchmark.domain.models import (
    BenchmarkExecutionSpecification,
    BenchmarkResult,
    CompiledEvalScopeBenchmark,
)
from autopilot.capabilities.benchmark.ports import BenchmarkAdapter
from autopilot.capabilities.benchmark.ports.lifecycle import BenchmarkStartContext
from autopilot.capabilities.capacity.domain.models import CapacityPlan
from autopilot.capabilities.deployment.domain.enums import DeploymentProviderState
from autopilot.capabilities.deployment.domain.errors import DeploymentProviderError
from autopilot.capabilities.deployment.domain.models import (
    CompiledVllmDeployment,
    DeploymentExecutionSpecification,
)
from autopilot.capabilities.deployment.ports import DeploymentAdapter
from autopilot.capabilities.deployment.ports.models import DeploymentStartContext
from autopilot.capabilities.environment.domain.models import EnvironmentInspectionResult
from autopilot.capabilities.evidence.domain.errors import EvidenceProviderError
from autopilot.capabilities.evidence.domain.models import (
    CodeRevision,
    EvidenceRunRef,
    EvidenceRunRequest,
)
from autopilot.capabilities.evidence.ports import EvidenceAdapter
from autopilot.capabilities.optimization.application.evaluator import (
    evaluate_slo,
    objective_value,
)
from autopilot.capabilities.optimization.application.search_space import (
    validate_trial_parameters,
)
from autopilot.capabilities.optimization.domain.enums import (
    TrialExecutionCode,
    TrialExecutionStage,
)
from autopilot.capabilities.optimization.domain.errors import (
    OptimizationValidationError,
    TrialExecutionError,
    TrialExecutionPendingError,
)
from autopilot.capabilities.optimization.domain.models import VllmSearchSpaceSpec
from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.base import StrictModel, UtcDatetime, utc_now
from autopilot.domain.candidates import DeploymentCandidate
from autopilot.domain.enums import (
    ErrorCategory,
    PlanKind,
    PlanStatus,
    SuggestedAction,
    TrialStatus,
)
from autopilot.domain.errors import DomainError, ErrorEnvelope, FieldError
from autopilot.domain.hashing import compute_content_hash
from autopilot.domain.identifiers import ExperimentId, Sha256Digest, StudyId, TrialId
from autopilot.domain.plans import PlanEnvelope
from autopilot.domain.trials import TrialRecord

INVALID_CAPACITY_BINDING = "Trial Candidate is not derived from the bound Capacity Plan"
INVALID_ENVIRONMENT_BINDING = "Trial Candidate is not bound to the inspected environment"
INVALID_DEPLOYMENT_BINDING = "compiled deployment does not match the Trial Candidate"
INVALID_BENCHMARK_BINDING = "compiled benchmark does not match the approved deployment"
INVALID_PLAN_BINDING = "Trial execution material does not match its approved Plan"
INVALID_SEARCH_BINDING = "Trial Search Space does not match the immutable SLO"
INVALID_DERIVED_CANDIDATE = "derived Candidate changed a fixed Capacity Plan field"
MEASURED_RESULT_MISMATCH = "measured Trial status and Benchmark result do not match"
TRACKING_RUN_MISMATCH = "Tracking Run does not belong to the terminal Trial"


@dataclass(frozen=True, slots=True)
class _TrialFailure:
    status: TrialStatus
    code: str
    category: ErrorCategory
    provider: str | None
    retryable: bool
    field: str | None = None
    evidence: tuple[ArtifactRef, ...] = ()


@dataclass(slots=True)
class _ActiveResources:
    deployment_started: bool = False
    benchmark_started: bool = False
    cleanup_required: bool = True


class TrialExecutionRequest(StrictModel):
    """Trusted worker input after Gateway authorization and plan loading."""

    schema_version: Literal["trial-execution-request/v1"] = "trial-execution-request/v1"
    experiment_id: ExperimentId
    study_id: StudyId
    trial_id: TrialId
    trial_number: int = Field(ge=0, le=1_000_000)
    environment: EnvironmentInspectionResult
    capacity_plan: CapacityPlan
    base_candidate: DeploymentCandidate
    candidate: DeploymentCandidate
    search_space: VllmSearchSpaceSpec | None = None
    deployment_plan: PlanEnvelope[DeploymentExecutionSpecification]
    deployment: CompiledVllmDeployment
    deployment_context: DeploymentStartContext
    benchmark_plan: PlanEnvelope[BenchmarkExecutionSpecification]
    benchmark: CompiledEvalScopeBenchmark
    benchmark_context: BenchmarkStartContext
    evidence_idempotency_key: Sha256Digest
    code_revision: CodeRevision
    started_at: UtcDatetime
    requirements_artifact: ArtifactRef | None = None
    workload_artifact: ArtifactRef | None = None
    logs_artifact: ArtifactRef | None = None

    @model_validator(mode="after")
    def validate_bindings(self) -> Self:
        passport = self.environment.hardware_passport
        base = self.base_candidate
        candidate = self.candidate
        if base not in self.capacity_plan.candidates:
            raise ValueError(INVALID_CAPACITY_BINDING)
        if (
            base.hardware_passport_id != passport.hardware_passport_id
            or base.hardware_passport_hash != compute_content_hash(passport)
            or base.hardware_passport_hash != self.capacity_plan.hardware_passport_hash
        ):
            raise ValueError(INVALID_ENVIRONMENT_BINDING)
        if (
            base.model_profile_id != self.capacity_plan.model_profile.model_profile_id
            or base.model_ref != self.capacity_plan.model_profile.model_ref
            or base.workload_hash != self.capacity_plan.workload_hash
        ):
            raise ValueError(INVALID_CAPACITY_BINDING)
        fixed_candidate_fields = (
            "profile",
            "hardware_passport_id",
            "hardware_passport_hash",
            "model_profile_id",
            "model_ref",
            "engine",
            "engine_image",
            "engine_version",
            "adapter_version",
            "workload_hash",
            "estimation",
        )
        if any(
            getattr(candidate, field) != getattr(base, field) for field in fixed_candidate_fields
        ):
            raise ValueError(INVALID_DERIVED_CANDIDATE)
        if self.search_space is None and candidate != base:
            raise ValueError(INVALID_DERIVED_CANDIDATE)
        benchmark_specification = self.benchmark_plan.execution_specification
        if self.search_space is not None and self.search_space.slo != benchmark_specification.slo:
            raise ValueError(INVALID_SEARCH_BINDING)
        deployment_specification = self.deployment_plan.execution_specification
        if (
            self.deployment_plan.experiment_id != self.experiment_id
            or self.deployment_plan.kind is not PlanKind.DEPLOYMENT
            or self.deployment_plan.status is not PlanStatus.APPROVED
            or self.deployment_plan.plan_id != self.deployment_context.plan_id
            or self.deployment_plan.plan_hash != self.deployment_context.plan_hash
            or deployment_specification.candidate != candidate
            or compute_content_hash(deployment_specification.workload) != candidate.workload_hash
        ):
            raise ValueError(INVALID_PLAN_BINDING)
        if (
            self.deployment.candidate_id != candidate.candidate_id
            or self.deployment.model_ref != candidate.model_ref
            or self.deployment.workload_hash != candidate.workload_hash
            or self.deployment.arguments.model_dump() != candidate.parameters.model_dump()
        ):
            raise ValueError(INVALID_DEPLOYMENT_BINDING)
        if (
            self.benchmark_plan.experiment_id != self.experiment_id
            or self.benchmark_plan.kind is not PlanKind.BENCHMARK
            or self.benchmark_plan.status is not PlanStatus.APPROVED
            or self.benchmark_plan.plan_id != self.benchmark_context.plan_id
            or self.benchmark_plan.plan_hash != self.benchmark_context.plan_hash
            or benchmark_specification.deployment_id != self.deployment_context.deployment_id
            or benchmark_specification.deployment_plan_hash != self.deployment_plan.plan_hash
            or benchmark_specification.workload != self.benchmark.workload
            or self.benchmark.deployment_id != self.deployment_context.deployment_id
            or self.benchmark.deployment_plan_hash != self.deployment_context.plan_hash
            or compute_content_hash(self.benchmark)
            != self.benchmark_context.compiled_spec_artifact.sha256
            or compute_content_hash(self.benchmark.workload) != candidate.workload_hash
        ):
            raise ValueError(INVALID_BENCHMARK_BINDING)
        return self


class TrialExecutionResult(StrictModel):
    """One terminal Trial plus its normalized result and Tracking reference."""

    schema_version: Literal["trial-execution-result/v1"] = "trial-execution-result/v1"
    trial: TrialRecord
    benchmark_result: BenchmarkResult | None = None
    evidence_run: EvidenceRunRef
    cleanup_completed: Literal[True] = True

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        measured = {
            TrialStatus.COMPLETED,
            TrialStatus.CONSTRAINT_FAILED,
            TrialStatus.OOM,
        }
        if (self.trial.status in measured) != (self.benchmark_result is not None):
            raise ValueError(MEASURED_RESULT_MISMATCH)
        if self.evidence_run.trial_id != self.trial.trial_id:
            raise ValueError(TRACKING_RUN_MISMATCH)
        return self


class FixedTrialExecutor:
    """Coordinate one Trial without implementing any Provider behavior."""

    def __init__(
        self,
        *,
        deployment: DeploymentAdapter,
        benchmark: BenchmarkAdapter,
        evidence: EvidenceAdapter,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._deployment = deployment
        self._benchmark = benchmark
        self._evidence = evidence
        self._clock = clock

    def execute(
        self,
        request: TrialExecutionRequest,
        *,
        cancellation_requested: Callable[[], bool] = lambda: False,
        active_stage: TrialExecutionStage | None = None,
    ) -> TrialExecutionResult:
        """Run until terminal or signal that an asynchronous Provider remains active."""
        resources = _ActiveResources(
            deployment_started=active_stage
            in {TrialExecutionStage.DEPLOYMENT, TrialExecutionStage.BENCHMARK},
            benchmark_started=active_stage is TrialExecutionStage.BENCHMARK,
        )
        try:
            trial, benchmark_result = self._run(
                request,
                resources,
                cancellation_requested,
            )
            return self._terminal_result(request, trial, benchmark_result)
        except TrialExecutionPendingError:
            resources.cleanup_required = False
            raise
        finally:
            if resources.cleanup_required:
                self._cleanup(
                    request,
                    benchmark_started=resources.benchmark_started,
                    deployment_started=resources.deployment_started,
                )

    def _run(
        self,
        request: TrialExecutionRequest,
        resources: _ActiveResources,
        cancellation_requested: Callable[[], bool],
    ) -> tuple[TrialRecord, BenchmarkResult | None]:
        if cancellation_requested():
            return self._trial(request, TrialStatus.CANCELLED), None
        static_failure = self._static_validation_failure(request)
        if static_failure is not None:
            return static_failure, None
        deployment_failure = self._run_deployment(request, resources, cancellation_requested)
        if deployment_failure is not None:
            return deployment_failure, None
        if cancellation_requested():
            return self._trial(request, TrialStatus.CANCELLED), None
        benchmark_step = self._run_benchmark(request, resources, cancellation_requested)
        if isinstance(benchmark_step, TrialRecord):
            return benchmark_step, None
        return self._measured_trial(request, benchmark_step), benchmark_step

    def _run_deployment(
        self,
        request: TrialExecutionRequest,
        resources: _ActiveResources,
        cancellation_requested: Callable[[], bool],
    ) -> TrialRecord | None:
        try:
            self._deployment.validate(request.deployment)
            resources.deployment_started = True
            operation = self._deployment.start(request.deployment, request.deployment_context)
            if operation.state in {
                DeploymentProviderState.ACCEPTED,
                DeploymentProviderState.RUNNING,
            }:
                operation = self._deployment.status(request.deployment_context)
            if cancellation_requested():
                return self._trial(request, TrialStatus.CANCELLED)
            if operation.state in {
                DeploymentProviderState.ACCEPTED,
                DeploymentProviderState.RUNNING,
            }:
                raise TrialExecutionPendingError(
                    TrialExecutionStage.DEPLOYMENT,
                    operation.provider_resource_id,
                )
            if operation.state is DeploymentProviderState.HEALTHY:
                return None
            failure = _TrialFailure(
                status=TrialStatus.DEPLOYMENT_FAILED,
                code="TRIAL_DEPLOYMENT_UNHEALTHY",
                category=ErrorCategory.DEPLOYMENT_ERROR,
                provider="vllm",
                retryable=False,
            )
        except DeploymentProviderError as error:
            failure = _TrialFailure(
                status=TrialStatus.DEPLOYMENT_FAILED,
                code=error.code.value,
                category=ErrorCategory.DEPLOYMENT_ERROR,
                provider="vllm",
                retryable=error.retryable,
            )
        return self._failed_trial(request, failure)

    def _run_benchmark(
        self,
        request: TrialExecutionRequest,
        resources: _ActiveResources,
        cancellation_requested: Callable[[], bool],
    ) -> BenchmarkResult | TrialRecord:
        try:
            self._benchmark.validate(request.benchmark)
            resources.benchmark_started = True
            operation = self._benchmark.start(request.benchmark, request.benchmark_context)
            if operation.state in {
                BenchmarkProviderState.ACCEPTED,
                BenchmarkProviderState.RUNNING,
            }:
                operation = self._benchmark.status(request.benchmark_context)
            if cancellation_requested() or operation.state is BenchmarkProviderState.CANCELLED:
                return self._trial(request, TrialStatus.CANCELLED)
            if operation.state in {
                BenchmarkProviderState.ACCEPTED,
                BenchmarkProviderState.RUNNING,
            }:
                raise TrialExecutionPendingError(
                    TrialExecutionStage.BENCHMARK,
                    operation.provider_resource_id,
                )
            if operation.state is not BenchmarkProviderState.SUCCEEDED:
                failure = _TrialFailure(
                    status=TrialStatus.BENCHMARK_FAILED,
                    code="TRIAL_BENCHMARK_PROVIDER_FAILED",
                    category=ErrorCategory.BENCHMARK_ERROR,
                    provider="evalscope",
                    retryable=False,
                )
                return self._failed_trial(request, failure)
            raw_report = self._benchmark.collect(request.benchmark_context)
            return self._benchmark.normalize(request.benchmark, raw_report)
        except (BenchmarkProviderError, BenchmarkValidationError) as error:
            failure = _TrialFailure(
                status=TrialStatus.BENCHMARK_FAILED,
                code=error.code.value,
                category=ErrorCategory.BENCHMARK_ERROR,
                provider="evalscope",
                retryable=(error.retryable if isinstance(error, BenchmarkProviderError) else False),
            )
            return self._failed_trial(request, failure)

    def _measured_trial(
        self,
        request: TrialExecutionRequest,
        result: BenchmarkResult,
    ) -> TrialRecord:
        raw_artifact = result.provenance.raw_artifact
        if result.oom:
            return self._failed_trial(
                request,
                _TrialFailure(
                    status=TrialStatus.OOM,
                    code="TRIAL_BENCHMARK_OOM",
                    category=ErrorCategory.OOM,
                    provider="evalscope",
                    retryable=False,
                    evidence=(raw_artifact,),
                ),
            )
        constraints = evaluate_slo(
            result,
            request.benchmark_plan.execution_specification.slo,
        )
        status = (
            TrialStatus.COMPLETED
            if all(evaluation.passed for evaluation in constraints)
            else TrialStatus.CONSTRAINT_FAILED
        )
        return TrialRecord(
            trial_id=request.trial_id,
            study_id=request.study_id,
            trial_number=request.trial_number,
            candidate_id=request.candidate.candidate_id,
            parameters=request.candidate.parameters,
            status=status,
            objective=objective_value(result),
            constraints=constraints,
            provenance=result.provenance,
            evidence=(raw_artifact,),
        )

    def _static_validation_failure(
        self,
        request: TrialExecutionRequest,
    ) -> TrialRecord | None:
        search_space = request.search_space
        if search_space is None:
            return None
        try:
            validate_trial_parameters(
                request.candidate.parameters,
                request.base_candidate.parameters,
                search_space,
                request.benchmark.workload,
            )
        except OptimizationValidationError as error:
            return self._failed_trial(
                request,
                _TrialFailure(
                    status=TrialStatus.REJECTED_STATIC,
                    code=error.code.value,
                    category=ErrorCategory.VALIDATION_ERROR,
                    provider=None,
                    retryable=False,
                    field=error.field,
                ),
            )
        return None

    def _terminal_result(
        self,
        request: TrialExecutionRequest,
        trial: TrialRecord,
        benchmark_result: BenchmarkResult | None = None,
    ) -> TrialExecutionResult:
        evidence_request = EvidenceRunRequest(
            experiment_id=request.experiment_id,
            candidate=request.candidate,
            trial=trial,
            benchmark_result=benchmark_result,
            hardware_passport_artifact=(
                request.environment.hardware_passport.provenance.raw_artifact
            ),
            model_profile_artifact=request.capacity_plan.model_profile.config_artifact,
            requirements_artifact=request.requirements_artifact,
            workload_artifact=request.workload_artifact,
            logs_artifact=request.logs_artifact,
            code_revision=request.code_revision,
            idempotency_key=request.evidence_idempotency_key,
            started_at=request.started_at,
            ended_at=max(request.started_at, self._clock()),
        )
        try:
            evidence_run = self._evidence.record_run(evidence_request)
        except EvidenceProviderError as error:
            raise TrialExecutionError(
                TrialExecutionCode.EVIDENCE_RECORDING_FAILED,
                "the terminal Trial could not be recorded by the Evidence Provider",
                retryable=error.retryable,
            ) from error
        return TrialExecutionResult(
            trial=trial,
            benchmark_result=benchmark_result,
            evidence_run=evidence_run,
        )

    @staticmethod
    def _trial(request: TrialExecutionRequest, status: TrialStatus) -> TrialRecord:
        return TrialRecord(
            trial_id=request.trial_id,
            study_id=request.study_id,
            trial_number=request.trial_number,
            candidate_id=request.candidate.candidate_id,
            parameters=request.candidate.parameters,
            status=status,
        )

    @staticmethod
    def _failed_trial(
        request: TrialExecutionRequest,
        failure: _TrialFailure,
    ) -> TrialRecord:
        field_errors = (FieldError(path=failure.field, reason="rejected"),) if failure.field else ()
        actions = (
            (SuggestedAction.REVISE_PLAN,)
            if failure.category is ErrorCategory.VALIDATION_ERROR
            else (SuggestedAction.CHECK_ENVIRONMENT,)
        )
        return TrialRecord(
            trial_id=request.trial_id,
            study_id=request.study_id,
            trial_number=request.trial_number,
            candidate_id=request.candidate.candidate_id,
            parameters=request.candidate.parameters,
            status=failure.status,
            evidence=failure.evidence,
            error=ErrorEnvelope(
                error=DomainError(
                    code=failure.code,
                    category=failure.category,
                    message="Trial execution failed at a classified deterministic boundary",
                    field_errors=field_errors,
                    retryable=failure.retryable,
                    provider=failure.provider,
                    suggested_actions=actions,
                )
            ),
        )

    def _cleanup(
        self,
        request: TrialExecutionRequest,
        *,
        benchmark_started: bool,
        deployment_started: bool,
    ) -> None:
        cleanup_failed = False
        retryable = False
        if benchmark_started:
            try:
                self._benchmark.cancel(request.benchmark_context)
            except BenchmarkProviderError as error:
                cleanup_failed = True
                retryable = retryable or error.retryable
        if deployment_started:
            try:
                self._deployment.cancel(request.deployment_context)
            except DeploymentProviderError as error:
                cleanup_failed = True
                retryable = retryable or error.retryable
        if cleanup_failed:
            raise TrialExecutionError(
                TrialExecutionCode.CLEANUP_FAILED,
                "one or more Trial Provider resources could not be cleaned up",
                retryable=retryable,
            )


__all__ = [
    "FixedTrialExecutor",
    "TrialExecutionRequest",
    "TrialExecutionResult",
]
