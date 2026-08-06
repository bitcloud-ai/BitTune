import hashlib

import pytest

from autopilot.capabilities.benchmark.adapters.evalscope import (
    EvalScopeRunnerAdapter,
    FakeEvalScopeAdapter,
)
from autopilot.capabilities.benchmark.domain.errors import BenchmarkProviderError
from autopilot.capabilities.benchmark.domain.models import (
    CompiledEvalScopeBenchmark,
    EvalScopeVersionProfile,
)
from autopilot.capabilities.benchmark.ports.lifecycle import BenchmarkStartContext
from autopilot.domain.artifacts import ArtifactProducer, ArtifactRef
from autopilot.domain.hashing import canonical_json_bytes
from autopilot.domain.identifiers import (
    ArtifactId,
    BenchmarkRunId,
    JobId,
    PlanHash,
    PlanId,
    Sha256Digest,
)


def _context(compiled: CompiledEvalScopeBenchmark) -> BenchmarkStartContext:
    data = canonical_json_bytes(compiled)
    digest = hashlib.sha256(data).hexdigest()
    suffix = "7" * 32
    return BenchmarkStartContext(
        benchmark_run_id=BenchmarkRunId(root=f"benchmark_{suffix}"),
        job_id=JobId(root=f"job_{suffix}"),
        plan_id=PlanId(root=f"plan_{'8' * 32}"),
        plan_hash=PlanHash(root=f"sha256:{'9' * 64}"),
        idempotency_key=Sha256Digest(root=f"sha256:{'a' * 64}"),
        request_id="request-benchmark-1",
        compiled_spec_artifact=ArtifactRef(
            artifact_id=ArtifactId(root=f"artifact_{'b' * 32}"),
            sha256=Sha256Digest(root=f"sha256:{digest}"),
            content_type="application/json",
            size_bytes=len(data),
            producer=ArtifactProducer(component="benchmark", version="test"),
        ),
    )


def test_fake_evalscope_adapter_runs_async_lifecycle_and_normalizes(
    compiled_open_loop_benchmark: CompiledEvalScopeBenchmark,
    evalscope_profile: EvalScopeVersionProfile,
) -> None:
    adapter = FakeEvalScopeAdapter(evalscope_profile)
    context = _context(compiled_open_loop_benchmark)

    started = adapter.start(compiled_open_loop_benchmark, context)
    status = adapter.status(context)
    report = adapter.collect(context)
    result = adapter.normalize(compiled_open_loop_benchmark, report)

    assert started.state == "running"
    assert status.state == "succeeded"
    assert result.reliability.success_rate == 1.0
    assert result.traffic_mode == compiled_open_loop_benchmark.traffic.mode


def test_evalscope_runner_adapter_fails_closed_without_verified_bindings(
    compiled_open_loop_benchmark: CompiledEvalScopeBenchmark,
) -> None:
    adapter = EvalScopeRunnerAdapter(
        profile=None,
        runner=None,
        locator=None,
        reports=None,
    )

    with pytest.raises(BenchmarkProviderError) as caught:
        adapter.validate(compiled_open_loop_benchmark)

    assert caught.value.retryable is False
