import hashlib
from collections.abc import Callable

import pytest

from autopilot.capabilities.benchmark.application.compiler import compile_benchmark
from autopilot.capabilities.benchmark.domain.enums import LatencyUnit
from autopilot.capabilities.benchmark.domain.models import (
    BenchmarkExecutionSpecification,
    CompiledEvalScopeBenchmark,
    EvalScopeMetricBinding,
    EvalScopeRawMetricBindings,
    EvalScopeVersionProfile,
    LatencyFieldBindings,
    LengthFieldBindings,
    OpenLoopSweepTraffic,
    PercentileFieldBindings,
    ReliabilityFieldBindings,
    TokenFieldBindings,
    TrafficSpec,
)
from autopilot.capabilities.benchmark.ports.models import (
    EvalScopeRawReport,
    RawMetricSample,
)
from autopilot.domain.artifacts import ArtifactProducer, ArtifactRef
from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.constraints import BooleanConstraint, NumericConstraint, SloSpec
from autopilot.domain.enums import BooleanMetric, NumericMetric, NumericOperator
from autopilot.domain.hashing import compute_content_hash
from autopilot.domain.identifiers import (
    ArtifactId,
    DeploymentId,
    ModelRevision,
    PlanHash,
    Sha256Digest,
)
from autopilot.domain.workloads import (
    SamplingSpec,
    SyntheticFixedDataset,
    TokenizerRef,
    WorkloadSpec,
)

BenchmarkSpecificationFactory = Callable[[TrafficSpec], BenchmarkExecutionSpecification]


def percentile_bindings(prefix: str) -> PercentileFieldBindings:
    return PercentileFieldBindings(
        p50=f"{prefix}_p50",
        p95=f"{prefix}_p95",
        p99=f"{prefix}_p99",
    )


@pytest.fixture
def evalscope_profile() -> EvalScopeVersionProfile:
    return EvalScopeVersionProfile(
        profile_version="evalscope-rtx5090-test-v1",
        provider_version="1.10.0-test",
        adapter_version="benchmark-adapter-test-v1",
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
                e2e=percentile_bindings("e2e_seconds"),
                ttft=percentile_bindings("ttft_seconds"),
                tpot=percentile_bindings("tpot_seconds"),
                itl=percentile_bindings("itl_seconds"),
            ),
            lengths=LengthFieldBindings(
                input_tokens=percentile_bindings("input_tokens"),
                output_tokens=percentile_bindings("output_tokens"),
            ),
        ),
        latency_unit=LatencyUnit.SECONDS,
    )


@pytest.fixture
def benchmark_specification_factory() -> BenchmarkSpecificationFactory:
    revision = ModelRevision(root="b" * 40)
    workload = WorkloadSpec(
        dataset=SyntheticFixedDataset(dataset_id="small-v1"),
        tokenizer=TokenizerRef(repository_id="Qwen/Qwen3-8B", revision=revision),
        prompt_tokens=100,
        output_tokens=20,
        stream=True,
        ignore_eos=True,
        sampling=SamplingSpec(seed=20_260_805),
    )
    slo = SloSpec(
        constraints=(
            NumericConstraint(
                metric=NumericMetric.TTFT_P95_MS,
                operator=NumericOperator.LESS_THAN_OR_EQUAL,
                value=2_000,
            ),
            NumericConstraint(
                metric=NumericMetric.SUCCESS_RATE,
                operator=NumericOperator.GREATER_THAN_OR_EQUAL,
                value=0.95,
            ),
            BooleanConstraint(),
        )
    )
    budget = ExecutionBudget(
        max_duration_seconds=1_800,
        max_requests=10_000,
        max_input_tokens=5_000_000,
        max_output_tokens=1_000_000,
        max_disk_growth_bytes=2_000_000_000,
    )

    def make_specification(traffic: TrafficSpec) -> BenchmarkExecutionSpecification:
        return BenchmarkExecutionSpecification(
            provider_version="1.10.0-test",
            adapter_version="benchmark-adapter-test-v1",
            provider_profile_version="evalscope-rtx5090-test-v1",
            budget=budget,
            deployment_id=DeploymentId(root=f"deployment_{'1' * 32}"),
            deployment_plan_hash=PlanHash(root=f"sha256:{'2' * 64}"),
            workload=workload,
            slo=slo,
            traffic=traffic,
        )

    return make_specification


@pytest.fixture
def compiled_open_loop_benchmark(
    benchmark_specification_factory: BenchmarkSpecificationFactory,
    evalscope_profile: EvalScopeVersionProfile,
) -> CompiledEvalScopeBenchmark:
    base_specification = benchmark_specification_factory(
        OpenLoopSweepTraffic(request_rates=(2.0,), duration_seconds=120)
    )
    workload = base_specification.workload.model_copy(
        update={"prompt_tokens": 2_000, "output_tokens": 500}
    )
    specification = base_specification.model_copy(update={"workload": workload})
    return compile_benchmark(specification, evalscope_profile)


@pytest.fixture
def evalscope_raw_report(
    compiled_open_loop_benchmark: CompiledEvalScopeBenchmark,
    evalscope_profile: EvalScopeVersionProfile,
) -> EvalScopeRawReport:
    values = {
        "submitted": 10,
        "completed": 8,
        "failed": 1,
        "timed_out": 1,
        "completed_within_window": 7,
        "scheduled_window_seconds": 120,
        "measurement_duration_seconds": 130,
        "successful_input_tokens": 16_000,
        "successful_output_tokens": 4_000,
        "e2e_seconds_p50": 1.0,
        "e2e_seconds_p95": 1.5,
        "e2e_seconds_p99": 2.0,
        "ttft_seconds_p50": 0.1,
        "ttft_seconds_p95": 0.2,
        "ttft_seconds_p99": 0.3,
        "tpot_seconds_p50": 0.01,
        "tpot_seconds_p95": 0.02,
        "tpot_seconds_p99": 0.03,
        "itl_seconds_p50": 0.01,
        "itl_seconds_p95": 0.02,
        "itl_seconds_p99": 0.04,
        "input_tokens_p50": 1_900,
        "input_tokens_p95": 2_000,
        "input_tokens_p99": 2_100,
        "output_tokens_p50": 450,
        "output_tokens_p95": 500,
        "output_tokens_p99": 500,
    }
    raw = b"evalscope-raw-report"
    artifact = ArtifactRef(
        artifact_id=ArtifactId(root=f"artifact_{'3' * 32}"),
        sha256=Sha256Digest(root=f"sha256:{hashlib.sha256(raw).hexdigest()}"),
        content_type="application/json",
        size_bytes=len(raw),
        producer=ArtifactProducer(
            component="evalscope-adapter",
            version="benchmark-adapter-test-v1",
        ),
    )
    return EvalScopeRawReport(
        provider_version="1.10.0-test",
        adapter_version="benchmark-adapter-test-v1",
        provider_profile_version="evalscope-rtx5090-test-v1",
        provider_profile_hash=compute_content_hash(evalscope_profile),
        compiled_benchmark_hash=compute_content_hash(compiled_open_loop_benchmark),
        traffic_mode=compiled_open_loop_benchmark.traffic.mode,
        metrics=tuple(RawMetricSample(name=name, value=value) for name, value in values.items()),
        oom=False,
        raw_artifact=artifact,
    )
