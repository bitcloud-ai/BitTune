from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from autopilot.capabilities.benchmark.domain.models import BenchmarkBudgetEstimate
from autopilot.capabilities.evidence.domain.models import ChampionPolicy, EvidenceRunRef
from autopilot.capabilities.optimization.adapters.fake import FakeOptimizationTrialRepository
from autopilot.capabilities.optimization.application.compiler import compile_optuna_study
from autopilot.capabilities.optimization.application.controller import (
    OptimizationController,
    OptimizationRunRequest,
)
from autopilot.capabilities.optimization.domain.enums import (
    OptimizationProviderTrialState,
    OptimizationRunState,
    OptimizationStopReason,
    TrialExecutionStage,
)
from autopilot.capabilities.optimization.domain.errors import TrialExecutionPendingError
from autopilot.capabilities.optimization.domain.models import (
    GpuMemoryUtilizationRange,
    OptimizationConvergencePolicy,
    OptimizationExecutionSpecification,
    OptimizationProviderTrial,
    OptimizationSuggestion,
    OptimizationTrialOutcome,
    OptunaStudyDefinition,
    OptunaVersionProfile,
    VllmSearchSpaceSpec,
)
from autopilot.capabilities.optimization.ports.models import TrialBudgetReservation
from autopilot.domain.artifacts import ArtifactProducer, ArtifactRef
from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.candidates import DeploymentCandidate, VllmTuningSpec
from autopilot.domain.constraints import (
    BooleanConstraint,
    ObjectiveSpec,
    SloSpec,
)
from autopilot.domain.enums import (
    BooleanMetric,
    Confidence,
    NumericMetric,
    PlanKind,
    PlanStatus,
    RiskLevel,
    TrialStatus,
)
from autopilot.domain.hashing import compute_content_hash
from autopilot.domain.identifiers import (
    ArtifactId,
    BenchmarkRunId,
    CandidateId,
    ExperimentId,
    HardwarePassportId,
    ImageDigest,
    ModelProfileId,
    ModelRevision,
    PlanId,
    Sha256Digest,
    StudyId,
)
from autopilot.domain.models import HuggingFaceModelRef
from autopilot.domain.plans import PlanEnvelope, compute_plan_envelope_hash
from autopilot.domain.provenance import EstimatedProvenance, MeasuredProvenance
from autopilot.domain.trials import (
    BooleanMetricValue,
    ConstraintEvaluation,
    NumericMetricValue,
    TrialRecord,
)
from autopilot.domain.workloads import (
    SamplingSpec,
    SyntheticFixedDataset,
    TokenizerRef,
    WorkloadSpec,
)


class FakeStudyAdapter:
    def __init__(self, profile: OptunaVersionProfile) -> None:
        self._profile = profile
        self._compiled = None
        self._trials: dict[int, OptimizationProviderTrial] = {}

    @property
    def profile(self) -> OptunaVersionProfile:
        return self._profile

    def create_or_load(self, study):
        self._compiled = study
        return SimpleNamespace(
            study_id=study.study_id,
            provider_study_name=study.provider_study_name,
            provider_version=self.profile.provider_version,
            adapter_version=self.profile.adapter_version,
            provider_profile_version=self.profile.profile_version,
            study_material_hash=compute_content_hash(study),
        )

    def ask(self, study, reference):
        del reference
        number = len(self._trials)
        index = number % len(study.configurations)
        suggestion = OptimizationSuggestion(
            study_id=study.study_id,
            trial_number=number,
            configuration_index=index,
            parameters=study.configurations[index],
        )
        self._trials[number] = OptimizationProviderTrial(
            suggestion=suggestion,
            state=OptimizationProviderTrialState.RUNNING,
        )
        return suggestion

    def tell(self, study, reference, outcome: OptimizationTrialOutcome):
        del study, reference
        current = self._trials[outcome.trial_number]
        if current.state is not OptimizationProviderTrialState.RUNNING:
            return current
        if outcome.status is TrialStatus.COMPLETED:
            updated = OptimizationProviderTrial(
                suggestion=current.suggestion,
                state=OptimizationProviderTrialState.COMPLETED,
                objective_value=outcome.objective_value,
                domain_status=outcome.status,
            )
        else:
            updated = OptimizationProviderTrial(
                suggestion=current.suggestion,
                state=OptimizationProviderTrialState.FAILED,
                domain_status=outcome.status,
            )
        self._trials[outcome.trial_number] = updated
        return updated

    def get_trials(self, study, reference):
        del study, reference
        return tuple(self._trials.values())


class FakeRequestFactory:
    def __init__(self, specification: OptimizationExecutionSpecification) -> None:
        self.specification = specification
        self.reservation = TrialBudgetReservation(
            requests=10,
            duration_seconds=10,
            input_tokens=20_480,
            output_tokens=5_120,
        )

    def estimate_reservation(self, specification):
        del specification
        return self.reservation

    def build_request(self, *, plan, suggestion, trial_id, candidate_id, started_at):
        del started_at
        candidate = self.specification.base_candidate.model_copy(
            update={"candidate_id": candidate_id, "parameters": suggestion.parameters}
        )
        suffix = trial_id.root.removeprefix("trial_")
        return SimpleNamespace(
            experiment_id=plan.experiment_id,
            study_id=suggestion.study_id,
            trial_id=trial_id,
            trial_number=suggestion.trial_number,
            base_candidate=self.specification.base_candidate,
            candidate=candidate,
            search_space=self.specification.definition.search_space,
            benchmark=SimpleNamespace(
                budget_estimate=BenchmarkBudgetEstimate(
                    measurement_requests=10,
                    warmup_requests=0,
                    total_requests=10,
                    estimated_duration_seconds=10,
                    estimated_input_tokens=20_480,
                    estimated_output_tokens=5_120,
                )
            ),
            benchmark_context=SimpleNamespace(
                benchmark_run_id=BenchmarkRunId(root=f"benchmark_{suffix}")
            ),
        )


class FakeExecutor:
    def __init__(self, artifact: ArtifactRef) -> None:
        self.artifact = artifact
        self.active_stages: list[TrialExecutionStage | None] = []

    def execute(self, request, *, cancellation_requested, active_stage=None):
        del cancellation_requested
        self.active_stages.append(active_stage)
        if request.trial_number == 3:
            trial = TrialRecord(
                trial_id=request.trial_id,
                study_id=request.study_id,
                trial_number=request.trial_number,
                candidate_id=request.candidate.candidate_id,
                parameters=request.candidate.parameters,
                status=TrialStatus.CANCELLED,
            )
        else:
            constraint = ConstraintEvaluation(
                constraint=BooleanConstraint(),
                observed=BooleanMetricValue(metric=BooleanMetric.OOM, value=False),
                passed=True,
            )
            trial = TrialRecord(
                trial_id=request.trial_id,
                study_id=request.study_id,
                trial_number=request.trial_number,
                candidate_id=request.candidate.candidate_id,
                parameters=request.candidate.parameters,
                status=TrialStatus.COMPLETED,
                objective=NumericMetricValue(
                    metric=NumericMetric.SUCCESSFUL_OUTPUT_TOKENS_PER_SECOND,
                    value=100 + request.trial_number,
                ),
                constraints=(constraint,),
                provenance=MeasuredProvenance(
                    provider="evalscope",
                    provider_version="test",
                    adapter_version="test",
                    raw_artifact=self.artifact,
                ),
                evidence=(self.artifact,),
            )
        return SimpleNamespace(
            trial=trial,
            evidence_run=EvidenceRunRef(
                provider_version="3.15.1",
                adapter_version="0.1.0",
                provider_profile_version="mlflow-v1",
                provider_run_id=f"run-{request.trial_id}",
                experiment_id=request.experiment_id,
                trial_id=request.trial_id,
                request_hash=Sha256Digest(root="sha256:" + "c" * 64),
            ),
        )


class PendingExecutor(FakeExecutor):
    def __init__(self, artifact: ArtifactRef) -> None:
        super().__init__(artifact)
        self.pending = True

    def execute(self, request, *, cancellation_requested, active_stage=None):
        if self.pending:
            self.active_stages.append(active_stage)
            self.pending = False
            raise TrialExecutionPendingError(TrialExecutionStage.BENCHMARK, "evalscope-job")
        return super().execute(
            request,
            cancellation_requested=cancellation_requested,
            active_stage=active_stage,
        )


def _artifact() -> ArtifactRef:
    return ArtifactRef(
        artifact_id=ArtifactId(root="artifact_" + "9" * 32),
        sha256=Sha256Digest(root="sha256:" + "d" * 64),
        content_type="application/json",
        size_bytes=1,
        producer=ArtifactProducer(component="test", version="1.0.0"),
    )


def _setup(*, max_requests: int = 1_000):
    artifact = _artifact()
    workload = WorkloadSpec(
        dataset=SyntheticFixedDataset(dataset_id="medium-v1"),
        tokenizer=TokenizerRef(
            repository_id="Qwen/Qwen3-8B", revision=ModelRevision(root="b" * 40)
        ),
        prompt_tokens=2_048,
        output_tokens=512,
        stream=True,
        ignore_eos=True,
        sampling=SamplingSpec(seed=20_260_805),
    )
    candidate = DeploymentCandidate(
        candidate_id=CandidateId.new(),
        profile="balanced",
        hardware_passport_id=HardwarePassportId.new(),
        hardware_passport_hash=Sha256Digest(root="sha256:" + "e" * 64),
        model_profile_id=ModelProfileId.new(),
        model_ref=HuggingFaceModelRef(
            repository_id="Qwen/Qwen3-8B", revision=ModelRevision(root="b" * 40)
        ),
        engine_image=ImageDigest(root="vllm/vllm@sha256:" + "f" * 64),
        engine_version="0.1.0",
        adapter_version="0.1.0",
        workload_hash=compute_content_hash(workload),
        parameters=VllmTuningSpec(
            max_model_len=8_192,
            gpu_memory_utilization=0.8,
            max_num_seqs=4,
            max_num_batched_tokens=8_192,
            enable_chunked_prefill=False,
        ),
        estimation=EstimatedProvenance(
            provider="llm-d-planner",
            provider_version="test",
            adapter_version="test",
            confidence=Confidence.MEDIUM,
            calculation_artifact=artifact,
        ),
    )
    definition = OptunaStudyDefinition(
        study_id=StudyId.new(),
        base_parameters=candidate.parameters,
        search_space=VllmSearchSpaceSpec(
            profile_name="controller-test-v1",
            objective=ObjectiveSpec(),
            slo=SloSpec(constraints=(BooleanConstraint(),)),
            gpu_memory_utilization=GpuMemoryUtilizationRange(low=0.8, high=0.82, step=0.02),
            max_num_seqs=(4, 8),
            max_num_batched_tokens=(4_096, 8_192),
            enable_chunked_prefill=(False, True),
        ),
        sampler_seed=20260805,
    )
    specification = OptimizationExecutionSpecification(
        provider_version="4.9.0",
        adapter_version="0.1.0",
        provider_profile_version="optuna-test-v1",
        budget=ExecutionBudget(
            max_duration_seconds=1_800,
            max_requests=max_requests,
            max_input_tokens=5_000_000,
            max_output_tokens=5_000_000,
            max_disk_growth_bytes=1_000_000,
        ),
        definition=definition,
        base_candidate=candidate,
        workload=workload,
        convergence=OptimizationConvergencePolicy(
            maximum_trials=10,
            no_improvement_trials=3,
            minimum_relative_improvement=0.01,
        ),
        champion_policy=ChampionPolicy(
            verification_repeats=2,
            max_coefficient_of_variation=0.05,
            noise_multiplier=1.0,
            minimum_relative_improvement=0.01,
        ),
    )
    experiment_id = ExperimentId.new()
    plan_id = PlanId.new()
    created_at = datetime.now(UTC)
    plan_hash = compute_plan_envelope_hash(
        plan_id=plan_id,
        experiment_id=experiment_id,
        kind=PlanKind.OPTIMIZATION,
        risk_level=RiskLevel.L2,
        execution_specification=specification,
    )
    plan = PlanEnvelope(
        plan_id=plan_id,
        experiment_id=experiment_id,
        kind=PlanKind.OPTIMIZATION,
        status=PlanStatus.APPROVED,
        risk_level=RiskLevel.L2,
        execution_specification=specification,
        plan_hash=plan_hash,
        created_at=created_at,
    )
    run_request = OptimizationRunRequest(plan=plan, started_at=created_at)
    profile = OptunaVersionProfile(
        profile_version="optuna-test-v1",
        provider_version="4.9.0",
        adapter_version="0.1.0",
    )
    adapter = FakeStudyAdapter(profile)
    repository = FakeOptimizationTrialRepository()
    factory = FakeRequestFactory(specification)
    executor = FakeExecutor(artifact)
    controller = OptimizationController(
        study_adapter=adapter,
        trial_repository=repository,
        trial_executor=executor,
        request_factory=factory,
    )
    return run_request, controller, adapter, repository, executor, factory


def test_controller_completes_ten_fake_trials_and_preserves_failure() -> None:
    request, controller, adapter, repository, executor, _ = _setup()

    results = [controller.advance(request) for _ in range(10)]
    final = results[-1]
    entries = repository.list_for_study(
        experiment_id=request.plan.experiment_id,
        study_id=request.plan.execution_specification.definition.study_id,
        plan_hash=request.plan.plan_hash,
    )

    assert final.progress.state is OptimizationRunState.STOPPED
    assert final.progress.stop_reason is OptimizationStopReason.TRIAL_BUDGET
    assert len(entries) == 10
    assert sum(entry.trial.status is TrialStatus.CANCELLED for entry in entries) == 1
    assert len(adapter._trials) == 10
    assert all(stage is None for stage in executor.active_stages)


def test_controller_resumes_pending_trial_and_cancellation_cleans_checkpoint() -> None:
    request, _, adapter, repository, _, factory = _setup()
    artifact = _artifact()
    executor = PendingExecutor(artifact)
    controller = OptimizationController(
        study_adapter=adapter,
        trial_repository=repository,
        trial_executor=executor,
        request_factory=factory,
    )

    pending = controller.advance(request)
    assert pending.progress.state is OptimizationRunState.PENDING
    cancelled = controller.advance(request, cancellation_requested=lambda: True)

    assert cancelled.progress.state is OptimizationRunState.STOPPED
    assert cancelled.progress.stop_reason is OptimizationStopReason.CANCELLED
    assert executor.active_stages[:2] == [None, TrialExecutionStage.BENCHMARK]


def test_controller_recovers_provider_trial_created_before_ledger_write() -> None:
    request, controller, adapter, repository, _, _ = _setup()
    specification = request.plan.execution_specification
    compiled = compile_optuna_study(
        specification.definition, request.plan.plan_hash, adapter.profile
    )
    reference = adapter.create_or_load(compiled)
    adapter.ask(compiled, reference)

    result = controller.advance(request)

    assert result.terminal_trial is not None
    assert result.terminal_trial.trial.trial_number == 0
    assert (
        len(
            repository.list_for_study(
                experiment_id=request.plan.experiment_id,
                study_id=specification.definition.study_id,
                plan_hash=request.plan.plan_hash,
            )
        )
        == 1
    )


def test_controller_stops_before_asking_when_request_budget_is_exhausted() -> None:
    request, controller, adapter, repository, _, _ = _setup(max_requests=5)

    result = controller.advance(request)

    assert result.progress.state is OptimizationRunState.STOPPED
    assert result.progress.stop_reason is OptimizationStopReason.REQUEST_BUDGET
    assert not adapter._trials
    assert not repository.list_for_study(
        experiment_id=request.plan.experiment_id,
        study_id=request.plan.execution_specification.definition.study_id,
        plan_hash=request.plan.plan_hash,
    )
