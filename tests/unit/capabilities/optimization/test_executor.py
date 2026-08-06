from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from autopilot.capabilities.benchmark.adapters.evalscope import FakeEvalScopeAdapter
from autopilot.capabilities.benchmark.application.compiler import compile_benchmark
from autopilot.capabilities.benchmark.domain.enums import (
    BenchmarkProviderState,
    BenchmarkValidationCode,
    LatencyUnit,
)
from autopilot.capabilities.benchmark.domain.errors import BenchmarkProviderError
from autopilot.capabilities.benchmark.domain.models import (
    BaselineTraffic,
    BenchmarkExecutionSpecification,
    CompiledEvalScopeBenchmark,
    EvalScopeMetricBinding,
    EvalScopeRawMetricBindings,
    EvalScopeVersionProfile,
    LatencyFieldBindings,
    LengthFieldBindings,
    PercentileFieldBindings,
    ReliabilityFieldBindings,
    TokenFieldBindings,
)
from autopilot.capabilities.benchmark.ports.lifecycle import (
    BenchmarkOperation,
    BenchmarkStartContext,
)
from autopilot.capabilities.benchmark.ports.models import EvalScopeRawReport
from autopilot.capabilities.capacity.adapters.fake import FakeCapacityPlannerAdapter
from autopilot.capabilities.capacity.domain.models import CapacityPlanningSpecification
from autopilot.capabilities.deployment.application.compiler import compile_deployment
from autopilot.capabilities.deployment.domain.enums import (
    DeploymentProviderState,
    DeploymentValidationCode,
)
from autopilot.capabilities.deployment.domain.errors import DeploymentProviderError
from autopilot.capabilities.deployment.domain.models import (
    CompiledVllmDeployment,
    DeploymentExecutionSpecification,
    VllmVersionProfile,
)
from autopilot.capabilities.deployment.ports.models import (
    DeploymentAdapterCapabilities,
    DeploymentOperation,
    DeploymentStartContext,
)
from autopilot.capabilities.environment.adapters.fake import FakeEnvironmentAdapter
from autopilot.capabilities.environment.domain.models import EnvironmentInspectionSpecification
from autopilot.capabilities.evidence.adapters.fake import FakeEvidenceAdapter
from autopilot.capabilities.evidence.domain.enums import EvidenceProviderState
from autopilot.capabilities.evidence.domain.models import EvidenceVersionProfile
from autopilot.capabilities.optimization.application.executor import (
    FixedTrialExecutor,
    TrialExecutionRequest,
)
from autopilot.capabilities.optimization.domain.errors import TrialExecutionPendingError
from autopilot.capabilities.optimization.domain.models import (
    GpuMemoryUtilizationRange,
    VllmSearchSpaceSpec,
)
from autopilot.domain.artifacts import ArtifactProducer, ArtifactRef
from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.constraints import (
    BooleanConstraint,
    NumericConstraint,
    ObjectiveSpec,
    SloSpec,
)
from autopilot.domain.enums import (
    BooleanMetric,
    ErrorCategory,
    NumericMetric,
    NumericOperator,
    PlanKind,
    PlanStatus,
    RiskLevel,
    TrialStatus,
)
from autopilot.domain.hashing import compute_content_hash
from autopilot.domain.identifiers import (
    ArtifactId,
    BenchmarkRunId,
    DeploymentId,
    ExperimentId,
    JobId,
    ModelRevision,
    PlanId,
    Sha256Digest,
    StudyId,
    TrialId,
    WorkerId,
)
from autopilot.domain.models import HuggingFaceModelRef
from autopilot.domain.plans import PlanEnvelope, compute_plan_envelope_hash
from autopilot.domain.workloads import (
    SamplingSpec,
    SyntheticFixedDataset,
    TokenizerRef,
    WorkloadSpec,
)

STARTED_AT = datetime(2026, 8, 6, 4, 0, tzinfo=UTC)


class FakeDeploymentAdapter:
    def __init__(self) -> None:
        self.start_calls = 0
        self.cancel_calls = 0
        self.fail_start = False
        self.status_state = DeploymentProviderState.HEALTHY

    def capabilities(self) -> DeploymentAdapterCapabilities:
        return DeploymentAdapterCapabilities()

    def validate(self, compiled: CompiledVllmDeployment) -> None:
        del compiled

    def start(
        self,
        compiled: CompiledVllmDeployment,
        context: DeploymentStartContext,
    ) -> DeploymentOperation:
        del compiled
        self.start_calls += 1
        if self.fail_start:
            raise DeploymentProviderError(
                DeploymentValidationCode.RUNNER_REJECTED,
                "fake deployment failed",
                retryable=False,
            )
        return self._operation(context, DeploymentProviderState.RUNNING)

    def status(self, context: DeploymentStartContext) -> DeploymentOperation:
        return self._operation(context, self.status_state)

    def cancel(self, context: DeploymentStartContext) -> DeploymentOperation:
        self.cancel_calls += 1
        return self._operation(context, DeploymentProviderState.STOPPED)

    @staticmethod
    def _operation(
        context: DeploymentStartContext,
        state: DeploymentProviderState,
    ) -> DeploymentOperation:
        return DeploymentOperation(
            deployment_id=context.deployment_id,
            state=state,
            provider_resource_id=str(context.deployment_id),
        )


class TrackingBenchmarkAdapter(FakeEvalScopeAdapter):
    def __init__(self, profile: EvalScopeVersionProfile) -> None:
        super().__init__(profile)
        self.start_calls = 0
        self.cancel_calls = 0
        self.fail_start = False
        self.report_oom = False
        self.remain_running = False

    def start(
        self,
        compiled: CompiledEvalScopeBenchmark,
        context: BenchmarkStartContext,
    ) -> BenchmarkOperation:
        self.start_calls += 1
        if self.fail_start:
            raise BenchmarkProviderError(
                BenchmarkValidationCode.RUNNER_REJECTED,
                "fake benchmark failed",
                retryable=False,
            )
        return super().start(compiled, context)

    def status(self, context: BenchmarkStartContext) -> BenchmarkOperation:
        if self.remain_running:
            return BenchmarkOperation(
                benchmark_run_id=context.benchmark_run_id,
                job_id=context.job_id,
                state=BenchmarkProviderState.RUNNING,
                provider_resource_id=str(context.job_id),
            )
        return super().status(context)

    def cancel(self, context: BenchmarkStartContext) -> BenchmarkOperation:
        self.cancel_calls += 1
        return super().cancel(context)

    def collect(self, context: BenchmarkStartContext) -> EvalScopeRawReport:
        report = super().collect(context)
        return report.model_copy(update={"oom": True}) if self.report_oom else report


def _workload() -> WorkloadSpec:
    revision = ModelRevision(root="b" * 40)
    return WorkloadSpec(
        dataset=SyntheticFixedDataset(dataset_id="medium-v1"),
        tokenizer=TokenizerRef(repository_id="Qwen/Qwen3-8B", revision=revision),
        prompt_tokens=2_048,
        output_tokens=512,
        stream=True,
        ignore_eos=True,
        sampling=SamplingSpec(seed=20_260_805),
    )


def _slo(*, ttft_p95_limit_ms: float = 2_000) -> SloSpec:
    return SloSpec(
        constraints=(
            NumericConstraint(
                metric=NumericMetric.TTFT_P95_MS,
                operator=NumericOperator.LESS_THAN_OR_EQUAL,
                value=ttft_p95_limit_ms,
            ),
            NumericConstraint(
                metric=NumericMetric.SUCCESS_RATE,
                operator=NumericOperator.GREATER_THAN_OR_EQUAL,
                value=0.95,
            ),
            BooleanConstraint(),
        )
    )


def _percentiles(prefix: str) -> PercentileFieldBindings:
    return PercentileFieldBindings(
        p50=f"{prefix}_p50",
        p95=f"{prefix}_p95",
        p99=f"{prefix}_p99",
    )


def _benchmark_profile() -> EvalScopeVersionProfile:
    return EvalScopeVersionProfile(
        profile_version="fake-evalscope-rtx5090-v1",
        provider_version="fake-evalscope-1.0.0",
        adapter_version="fake-benchmark-adapter-v1",
        rtx_5090_verified=True,
        number_safety_factor=1.1,
        warmup_ratio=0.1,
        rest_between_levels_seconds=2,
        closed_loop_level_timeout_seconds=60,
        completion_grace_seconds=10,
        max_request_rate_rps=100,
        max_closed_loop_concurrency=64,
        sla_rate_parameter="request_rate",
        sla_concurrency_parameter="parallel",
        sla_metric_bindings=(
            EvalScopeMetricBinding(
                metric=NumericMetric.TTFT_P95_MS,
                provider_name="ttft_p95_ms",
            ),
            EvalScopeMetricBinding(
                metric=NumericMetric.SUCCESS_RATE,
                provider_name="success_rate",
            ),
            EvalScopeMetricBinding(metric=BooleanMetric.OOM, provider_name="oom"),
        ),
        raw_metric_bindings=EvalScopeRawMetricBindings(
            reliability=ReliabilityFieldBindings(
                submitted="submitted",
                completed="completed",
                failed="failed",
                timed_out="timed_out",
                completed_within_window="completed_within_window",
                scheduled_window_seconds="scheduled_window_seconds",
                measurement_duration_seconds="measurement_duration_seconds",
            ),
            tokens=TokenFieldBindings(
                successful_input_tokens="successful_input_tokens",
                successful_output_tokens="successful_output_tokens",
            ),
            latency=LatencyFieldBindings(
                e2e=_percentiles("e2e_seconds"),
                ttft=_percentiles("ttft_seconds"),
                tpot=_percentiles("tpot_seconds"),
                itl=_percentiles("itl_seconds"),
            ),
            lengths=LengthFieldBindings(
                input_tokens=_percentiles("input_tokens"),
                output_tokens=_percentiles("output_tokens"),
            ),
        ),
        latency_unit=LatencyUnit.SECONDS,
    )


def _compiled_artifact(compiled: CompiledEvalScopeBenchmark) -> ArtifactRef:
    data = compiled.model_dump_json().encode("utf-8")
    digest = compute_content_hash(compiled)
    return ArtifactRef(
        artifact_id=ArtifactId(
            root="artifact_" + hashlib.sha256(data).hexdigest()[:32],
        ),
        sha256=digest,
        content_type="application/json",
        size_bytes=len(data),
        producer=ArtifactProducer(component="benchmark", version="test"),
    )


def _request(
    *,
    slo: SloSpec | None = None,
) -> tuple[
    TrialExecutionRequest,
    FakeDeploymentAdapter,
    TrackingBenchmarkAdapter,
    FakeEvidenceAdapter,
]:
    workload = _workload()
    environment_adapter = FakeEnvironmentAdapter()
    environment = environment_adapter.inspect(
        EnvironmentInspectionSpecification(
            provider_version=environment_adapter.profile.provider_version,
            adapter_version=environment_adapter.profile.adapter_version,
            provider_profile_version=environment_adapter.profile.profile_version,
        )
    )
    capacity_adapter = FakeCapacityPlannerAdapter()
    capacity_plan = capacity_adapter.create_plan(
        CapacityPlanningSpecification(
            provider_version=capacity_adapter.profile.provider_version,
            adapter_version=capacity_adapter.profile.adapter_version,
            provider_profile_version=capacity_adapter.profile.profile_version,
            model_ref=HuggingFaceModelRef(
                repository_id="Qwen/Qwen3-8B",
                revision=ModelRevision(root="b" * 40),
            ),
            hardware_passport=environment.hardware_passport,
            workload=workload,
            requested_max_model_len=8_192,
            requested_gpu_memory_utilization=0.90,
            expected_concurrency=8,
        )
    )
    candidate = capacity_plan.candidates[1]
    experiment_id = ExperimentId(root="exp_" + "8" * 32)
    deployment_profile = VllmVersionProfile(
        profile_version="fake-vllm-rtx5090-v1",
        provider_version=candidate.engine_version,
        adapter_version=candidate.adapter_version,
        engine_image=candidate.engine_image,
        rtx_5090_verified=True,
        max_model_len_upper_bound=32_768,
        gpu_memory_utilization_min=0.80,
        gpu_memory_utilization_max=0.94,
        supported_max_num_seqs=(4, 8, 16, 32),
        supported_max_num_batched_tokens=(2_048, 4_096, 8_192, 16_384),
        supports_chunked_prefill=True,
        container_port=8_000,
        pid_limit=1_024,
        startup_timeout_seconds=60,
        max_task_timeout_seconds=1_800,
        health_check_timeout_seconds=30,
    )
    deployment_specification = DeploymentExecutionSpecification(
        provider_version=deployment_profile.provider_version,
        adapter_version=deployment_profile.adapter_version,
        provider_profile_version=deployment_profile.profile_version,
        budget=ExecutionBudget(
            max_duration_seconds=600,
            max_requests=1,
            max_input_tokens=1,
            max_output_tokens=1,
            max_disk_growth_bytes=10_000_000_000,
        ),
        candidate=candidate,
        workload=workload,
    )
    compiled_deployment = compile_deployment(deployment_specification, deployment_profile)
    deployment_plan_id = PlanId(root="plan_" + "2" * 32)
    deployment_plan_hash = compute_plan_envelope_hash(
        plan_id=deployment_plan_id,
        experiment_id=experiment_id,
        kind=PlanKind.DEPLOYMENT,
        risk_level=RiskLevel.L2,
        execution_specification=deployment_specification,
    )
    deployment_plan = PlanEnvelope[DeploymentExecutionSpecification](
        plan_id=deployment_plan_id,
        experiment_id=experiment_id,
        kind=PlanKind.DEPLOYMENT,
        status=PlanStatus.APPROVED,
        risk_level=RiskLevel.L2,
        execution_specification=deployment_specification,
        plan_hash=deployment_plan_hash,
        created_at=STARTED_AT - timedelta(minutes=5),
    )
    deployment_context = DeploymentStartContext(
        deployment_id=DeploymentId(root="deployment_" + "1" * 32),
        plan_id=deployment_plan.plan_id,
        plan_hash=deployment_plan_hash,
        idempotency_key=Sha256Digest(root="sha256:" + "3" * 64),
        worker_id=WorkerId(root="worker_" + "4" * 32),
        request_id="fixed-trial-deployment",
    )
    benchmark_profile = _benchmark_profile()
    benchmark_specification = BenchmarkExecutionSpecification(
        provider_version=benchmark_profile.provider_version,
        adapter_version=benchmark_profile.adapter_version,
        provider_profile_version=benchmark_profile.profile_version,
        budget=ExecutionBudget(
            max_duration_seconds=300,
            max_requests=100,
            max_input_tokens=500_000,
            max_output_tokens=100_000,
            max_disk_growth_bytes=1_000_000_000,
        ),
        deployment_id=deployment_context.deployment_id,
        deployment_plan_hash=deployment_plan_hash,
        workload=workload,
        slo=slo or _slo(),
        traffic=BaselineTraffic(requests=5),
    )
    compiled_benchmark = compile_benchmark(benchmark_specification, benchmark_profile)
    suffix = "5" * 32
    benchmark_plan_id = PlanId(root="plan_" + "6" * 32)
    benchmark_plan_hash = compute_plan_envelope_hash(
        plan_id=benchmark_plan_id,
        experiment_id=experiment_id,
        kind=PlanKind.BENCHMARK,
        risk_level=RiskLevel.L1,
        execution_specification=benchmark_specification,
    )
    benchmark_plan = PlanEnvelope[BenchmarkExecutionSpecification](
        plan_id=benchmark_plan_id,
        experiment_id=experiment_id,
        kind=PlanKind.BENCHMARK,
        status=PlanStatus.APPROVED,
        risk_level=RiskLevel.L1,
        execution_specification=benchmark_specification,
        plan_hash=benchmark_plan_hash,
        created_at=STARTED_AT - timedelta(minutes=4),
    )
    benchmark_context = BenchmarkStartContext(
        benchmark_run_id=BenchmarkRunId(root="benchmark_" + suffix),
        job_id=JobId(root="job_" + suffix),
        plan_id=benchmark_plan.plan_id,
        plan_hash=benchmark_plan.plan_hash,
        idempotency_key=Sha256Digest(root="sha256:" + "7" * 64),
        request_id="fixed-trial-benchmark",
        compiled_spec_artifact=_compiled_artifact(compiled_benchmark),
    )
    request = TrialExecutionRequest(
        experiment_id=experiment_id,
        study_id=StudyId(root="study_" + "9" * 32),
        trial_id=TrialId(root="trial_" + "a" * 32),
        trial_number=0,
        environment=environment,
        capacity_plan=capacity_plan,
        base_candidate=candidate,
        candidate=candidate,
        deployment_plan=deployment_plan,
        deployment=compiled_deployment,
        deployment_context=deployment_context,
        benchmark_plan=benchmark_plan,
        benchmark=compiled_benchmark,
        benchmark_context=benchmark_context,
        evidence_idempotency_key=Sha256Digest(root="sha256:" + "b" * 64),
        code_revision="c" * 40,
        started_at=STARTED_AT,
    )
    deployment = FakeDeploymentAdapter()
    benchmark = TrackingBenchmarkAdapter(benchmark_profile)
    evidence = FakeEvidenceAdapter(
        EvidenceVersionProfile(
            profile_version="fake-mlflow-v1",
            provider_version="3.15.1",
            adapter_version="fake-evidence-adapter-v1",
        )
    )
    return request, deployment, benchmark, evidence


def _execute(
    request: TrialExecutionRequest,
    deployment: FakeDeploymentAdapter,
    benchmark: TrackingBenchmarkAdapter,
    evidence: FakeEvidenceAdapter,
):
    executor = FixedTrialExecutor(
        deployment=deployment,
        benchmark=benchmark,
        evidence=evidence,
        clock=lambda: STARTED_AT + timedelta(seconds=90),
    )
    return executor.execute(request)


def test_fixed_trial_closes_fake_environment_capacity_benchmark_and_evidence() -> None:
    request, deployment, benchmark, evidence = _request()

    result = _execute(request, deployment, benchmark, evidence)

    assert result.trial.status is TrialStatus.COMPLETED
    assert result.benchmark_result is not None
    assert result.trial.evidence == (result.benchmark_result.provenance.raw_artifact,)
    assert evidence.get_run_status(result.evidence_run).state is EvidenceProviderState.SUCCEEDED
    assert deployment.start_calls == 1
    assert deployment.cancel_calls == 1
    assert benchmark.start_calls == 1
    assert benchmark.cancel_calls == 1


def test_static_rejection_never_starts_deployment() -> None:
    request, deployment, benchmark, evidence = _request()
    search_space = VllmSearchSpaceSpec(
        profile_name="fixed-test-v1",
        objective=ObjectiveSpec(),
        slo=request.benchmark_plan.execution_specification.slo,
        gpu_memory_utilization=GpuMemoryUtilizationRange(low=0.88, high=0.92, step=0.02),
        max_num_seqs=(4,),
        max_num_batched_tokens=(4_096,),
        enable_chunked_prefill=(True,),
    )
    payload = request.model_dump(mode="python")
    payload.update(
        {
            "search_space": search_space,
            "trial_id": TrialId(root="trial_" + "e" * 32),
            "evidence_idempotency_key": Sha256Digest(root="sha256:" + "f" * 64),
        }
    )
    derived_request = TrialExecutionRequest.model_validate(payload)

    result = _execute(derived_request, deployment, benchmark, evidence)

    assert result.trial.status is TrialStatus.REJECTED_STATIC
    assert result.trial.error is not None
    assert result.trial.error.error.category is ErrorCategory.VALIDATION_ERROR
    assert deployment.start_calls == 0
    assert deployment.cancel_calls == 0
    assert benchmark.start_calls == 0


def test_deployment_failure_is_recorded_and_cleanup_is_attempted() -> None:
    request, deployment, benchmark, evidence = _request()
    deployment.fail_start = True

    result = _execute(request, deployment, benchmark, evidence)

    assert result.trial.status is TrialStatus.DEPLOYMENT_FAILED
    assert result.trial.error is not None
    assert result.trial.error.error.code == DeploymentValidationCode.RUNNER_REJECTED.value
    assert evidence.get_run_status(result.evidence_run).state is EvidenceProviderState.FAILED
    assert deployment.cancel_calls == 1
    assert benchmark.start_calls == 0


def test_benchmark_failure_cleans_up_benchmark_then_deployment() -> None:
    request, deployment, benchmark, evidence = _request()
    benchmark.fail_start = True

    result = _execute(request, deployment, benchmark, evidence)

    assert result.trial.status is TrialStatus.BENCHMARK_FAILED
    assert result.trial.error is not None
    assert result.trial.error.error.code == BenchmarkValidationCode.RUNNER_REJECTED.value
    assert benchmark.cancel_calls == 1
    assert deployment.cancel_calls == 1


def test_oom_result_keeps_raw_evidence_without_objective() -> None:
    request, deployment, benchmark, evidence = _request()
    benchmark.report_oom = True

    result = _execute(request, deployment, benchmark, evidence)

    assert result.trial.status is TrialStatus.OOM
    assert result.trial.objective is None
    assert result.trial.error is not None
    assert result.trial.error.error.category is ErrorCategory.OOM
    assert result.benchmark_result is not None
    assert result.trial.evidence == (result.benchmark_result.provenance.raw_artifact,)


def test_constraint_failure_retains_measured_objective() -> None:
    request, deployment, benchmark, evidence = _request(slo=_slo(ttft_p95_limit_ms=100))

    result = _execute(request, deployment, benchmark, evidence)

    assert result.trial.status is TrialStatus.CONSTRAINT_FAILED
    assert result.trial.objective is not None
    assert any(not constraint.passed for constraint in result.trial.constraints)
    assert evidence.get_run_status(result.evidence_run).state is EvidenceProviderState.SUCCEEDED


def test_pre_start_cancellation_records_terminal_trial_without_provider_work() -> None:
    request, deployment, benchmark, evidence = _request()
    executor = FixedTrialExecutor(
        deployment=deployment,
        benchmark=benchmark,
        evidence=evidence,
        clock=lambda: STARTED_AT,
    )

    result = executor.execute(request, cancellation_requested=lambda: True)

    assert result.trial.status is TrialStatus.CANCELLED
    assert evidence.get_run_status(result.evidence_run).state is EvidenceProviderState.CANCELLED
    assert deployment.start_calls == 0
    assert benchmark.start_calls == 0


def test_running_benchmark_signals_pending_without_destroying_active_resources() -> None:
    request, deployment, benchmark, evidence = _request()
    benchmark.remain_running = True
    executor = FixedTrialExecutor(
        deployment=deployment,
        benchmark=benchmark,
        evidence=evidence,
    )

    with pytest.raises(TrialExecutionPendingError) as caught:
        executor.execute(request)

    assert caught.value.stage.value == "benchmark"
    assert benchmark.cancel_calls == 0
    assert deployment.cancel_calls == 0


def test_trial_request_rejects_candidate_outside_capacity_provenance() -> None:
    request, _, _, _ = _request()
    foreign = request.candidate.model_copy(
        update={
            "model_ref": HuggingFaceModelRef(
                repository_id="Qwen/Qwen3-14B",
                revision=ModelRevision(root="d" * 40),
            )
        }
    )

    with pytest.raises(ValueError, match="fixed Capacity Plan field"):
        TrialExecutionRequest.model_validate(
            {**request.model_dump(mode="python"), "candidate": foreign}
        )
