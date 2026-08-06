"""Pinned EvalScope Python-API adapter over the typed Host Runner."""

from __future__ import annotations

import hashlib
from typing import Protocol

from autopilot.capabilities.benchmark.application.normalizer import normalize_evalscope_report
from autopilot.capabilities.benchmark.domain.enums import (
    BenchmarkProviderState,
    BenchmarkValidationCode,
    LatencyUnit,
)
from autopilot.capabilities.benchmark.domain.errors import BenchmarkProviderError
from autopilot.capabilities.benchmark.domain.models import (
    BenchmarkResult,
    CompiledEvalScopeBenchmark,
    EvalScopeVersionProfile,
)
from autopilot.capabilities.benchmark.ports import (
    BenchmarkArtifactLocator,
    BenchmarkReportReader,
)
from autopilot.capabilities.benchmark.ports.lifecycle import (
    BenchmarkAdapterCapabilities,
    BenchmarkOperation,
    BenchmarkStartContext,
)
from autopilot.capabilities.benchmark.ports.models import EvalScopeRawReport, RawMetricSample
from autopilot.domain.artifacts import ArtifactProducer, ArtifactRef
from autopilot.domain.hashing import compute_content_hash
from autopilot.domain.identifiers import ArtifactId, JobId, Sha256Digest
from runner.models import (
    BenchmarkStartPayload,
    CancelBenchmarkRequest,
    JobArtifactsRequest,
    JobRefPayload,
    JobStatusRequest,
    RelativeStoragePath,
    RunnerArtifactInput,
    RunnerRequest,
    RunnerResponse,
    StartBenchmarkRequest,
    StorageRef,
    StorageRoot,
)
from runner.models import Sha256Digest as RunnerDigest

LATENCY_GROUP_COUNT = 4


class RunnerDispatcher(Protocol):
    def dispatch(self, request: RunnerRequest) -> RunnerResponse: ...


class EvalScopeRunnerAdapter:
    """Run only a profile-bound compiled benchmark through the Runner."""

    def __init__(
        self,
        *,
        profile: EvalScopeVersionProfile | None,
        runner: RunnerDispatcher | None,
        locator: BenchmarkArtifactLocator | None,
        reports: BenchmarkReportReader | None,
    ) -> None:
        self._profile = profile
        self._runner = runner
        self._locator = locator
        self._reports = reports

    def capabilities(self) -> BenchmarkAdapterCapabilities:
        return BenchmarkAdapterCapabilities()

    def validate(self, compiled: CompiledEvalScopeBenchmark) -> None:
        profile = self._profile
        if (
            profile is None
            or self._runner is None
            or self._locator is None
            or self._reports is None
        ):
            raise BenchmarkProviderError(
                BenchmarkValidationCode.PROVIDER_UNAVAILABLE,
                "the G0-verified EvalScope profile and Runner bindings are not configured",
                retryable=False,
            )
        if (
            compiled.provider_version != profile.provider_version
            or compiled.adapter_version != profile.adapter_version
            or compiled.provider_profile_version != profile.profile_version
        ):
            raise BenchmarkProviderError(
                BenchmarkValidationCode.PROFILE_UNVERIFIED,
                "compiled benchmark does not match the registered EvalScope profile",
                retryable=False,
            )

    def start(
        self,
        compiled: CompiledEvalScopeBenchmark,
        context: BenchmarkStartContext,
    ) -> BenchmarkOperation:
        self.validate(compiled)
        if context.compiled_spec_artifact.sha256 != compute_content_hash(compiled):
            raise BenchmarkProviderError(
                BenchmarkValidationCode.ARTIFACT_BINDING,
                "compiled benchmark Artifact does not match the approved provider DTO",
                retryable=False,
            )
        location = self._require_locator().locate_for_runner(context.compiled_spec_artifact)
        request = StartBenchmarkRequest(
            request_id=context.request_id,
            idempotency_key=RunnerDigest(root=context.idempotency_key.root),
            actor="autopilot-worker",
            plan_id=str(context.plan_id),
            plan_hash=RunnerDigest(root=context.plan_hash.root),
            payload=BenchmarkStartPayload(
                benchmark_id=str(context.benchmark_run_id),
                deployment_id=str(compiled.deployment_id),
                compiled_spec_artifact=RunnerArtifactInput(
                    artifact_id=str(context.compiled_spec_artifact.artifact_id),
                    sha256=RunnerDigest(root=context.compiled_spec_artifact.sha256.root),
                    size_bytes=context.compiled_spec_artifact.size_bytes,
                    storage=StorageRef(
                        root=StorageRoot.OUTPUT,
                        relative_path=RelativeStoragePath(root=location.storage_key),
                    ),
                ),
                max_duration_seconds=compiled.execution_budget.max_duration_seconds,
                max_requests=compiled.execution_budget.max_requests,
                max_input_tokens=compiled.execution_budget.max_input_tokens,
                max_output_tokens=compiled.execution_budget.max_output_tokens,
                cpu_millis=2_000,
                max_memory_bytes=4_294_967_296,
                pid_limit=512,
                max_disk_growth_bytes=compiled.execution_budget.max_disk_growth_bytes,
            ),
        )
        return self._dispatch(request, context)

    def status(self, context: BenchmarkStartContext) -> BenchmarkOperation:
        request = JobStatusRequest(
            request_id=context.request_id,
            idempotency_key=RunnerDigest(root=context.idempotency_key.root),
            actor="autopilot-worker",
            plan_id=str(context.plan_id),
            plan_hash=RunnerDigest(root=context.plan_hash.root),
            payload=JobRefPayload(job_id=str(context.job_id)),
        )
        return self._dispatch(request, context)

    def cancel(self, context: BenchmarkStartContext) -> BenchmarkOperation:
        request = CancelBenchmarkRequest(
            request_id=context.request_id,
            idempotency_key=RunnerDigest(root=context.idempotency_key.root),
            actor="autopilot-worker",
            plan_id=str(context.plan_id),
            plan_hash=RunnerDigest(root=context.plan_hash.root),
            payload=JobRefPayload(job_id=str(context.job_id)),
        )
        return self._dispatch(request, context)

    def collect(self, context: BenchmarkStartContext) -> EvalScopeRawReport:
        request = JobArtifactsRequest(
            request_id=context.request_id,
            idempotency_key=RunnerDigest(root=context.idempotency_key.root),
            actor="autopilot-worker",
            plan_id=str(context.plan_id),
            plan_hash=RunnerDigest(root=context.plan_hash.root),
            payload=JobRefPayload(job_id=str(context.job_id)),
        )
        self._dispatch(request, context)
        reports = self._reports
        if reports is None:
            raise BenchmarkProviderError(
                BenchmarkValidationCode.PROVIDER_UNAVAILABLE,
                "the EvalScope report reader is not configured",
                retryable=False,
            )
        return reports.read_evalscope_report(context.job_id)

    def normalize(
        self,
        compiled: CompiledEvalScopeBenchmark,
        report: EvalScopeRawReport,
    ) -> BenchmarkResult:
        profile = self._profile
        if profile is None:
            raise BenchmarkProviderError(
                BenchmarkValidationCode.PROVIDER_UNAVAILABLE,
                "the EvalScope profile is not configured",
                retryable=False,
            )
        return normalize_evalscope_report(report, compiled, profile)

    def _dispatch(
        self,
        request: RunnerRequest,
        context: BenchmarkStartContext,
    ) -> BenchmarkOperation:
        runner = self._runner
        if runner is None:
            raise BenchmarkProviderError(
                BenchmarkValidationCode.PROVIDER_UNAVAILABLE,
                "the typed Host Runner client is not configured",
                retryable=False,
            )
        response = runner.dispatch(request)
        if not response.accepted or response.result is None:
            error = response.error
            raise BenchmarkProviderError(
                BenchmarkValidationCode.RUNNER_REJECTED,
                error.message if error is not None else "Host Runner rejected the benchmark",
                retryable=error.retryable if error is not None else False,
            )
        state_map = {
            "accepted": BenchmarkProviderState.ACCEPTED,
            "running": BenchmarkProviderState.RUNNING,
            "succeeded": BenchmarkProviderState.SUCCEEDED,
            "stopped": BenchmarkProviderState.CANCELLED,
            "cancelled": BenchmarkProviderState.CANCELLED,
            "failed": BenchmarkProviderState.FAILED,
        }
        return BenchmarkOperation(
            benchmark_run_id=context.benchmark_run_id,
            job_id=context.job_id,
            state=state_map[response.result.state],
            provider_resource_id=response.result.resource_id,
            idempotent_replay=response.idempotent_replay,
            detail=response.result.detail,
        )

    def _require_locator(self) -> BenchmarkArtifactLocator:
        if self._locator is None:
            raise BenchmarkProviderError(
                BenchmarkValidationCode.PROVIDER_UNAVAILABLE,
                "the Runner Artifact locator is not configured",
                retryable=False,
            )
        return self._locator


class FakeEvalScopeAdapter:
    """Deterministic asynchronous EvalScope fake for Graph and API tests."""

    def __init__(self, profile: EvalScopeVersionProfile) -> None:
        self._profile = profile
        self._reports: dict[JobId, EvalScopeRawReport] = {}

    def capabilities(self) -> BenchmarkAdapterCapabilities:
        return BenchmarkAdapterCapabilities()

    def validate(self, compiled: CompiledEvalScopeBenchmark) -> None:
        if (
            compiled.provider_version != self._profile.provider_version
            or compiled.adapter_version != self._profile.adapter_version
            or compiled.provider_profile_version != self._profile.profile_version
        ):
            raise BenchmarkProviderError(
                BenchmarkValidationCode.PROFILE_UNVERIFIED,
                "compiled benchmark does not match the Fake EvalScope profile",
                retryable=False,
            )

    def start(
        self,
        compiled: CompiledEvalScopeBenchmark,
        context: BenchmarkStartContext,
    ) -> BenchmarkOperation:
        self.validate(compiled)
        replay = context.job_id in self._reports
        if not replay:
            self._reports[context.job_id] = self._fake_report(compiled)
        return BenchmarkOperation(
            benchmark_run_id=context.benchmark_run_id,
            job_id=context.job_id,
            state=BenchmarkProviderState.RUNNING,
            provider_resource_id=str(context.benchmark_run_id),
            idempotent_replay=replay,
        )

    def status(self, context: BenchmarkStartContext) -> BenchmarkOperation:
        state = (
            BenchmarkProviderState.SUCCEEDED
            if context.job_id in self._reports
            else BenchmarkProviderState.FAILED
        )
        return BenchmarkOperation(
            benchmark_run_id=context.benchmark_run_id,
            job_id=context.job_id,
            state=state,
            provider_resource_id=str(context.job_id),
        )

    def cancel(self, context: BenchmarkStartContext) -> BenchmarkOperation:
        self._reports.pop(context.job_id, None)
        return BenchmarkOperation(
            benchmark_run_id=context.benchmark_run_id,
            job_id=context.job_id,
            state=BenchmarkProviderState.CANCELLED,
            provider_resource_id=str(context.job_id),
        )

    def collect(self, context: BenchmarkStartContext) -> EvalScopeRawReport:
        try:
            return self._reports[context.job_id]
        except KeyError as error:
            raise BenchmarkProviderError(
                BenchmarkValidationCode.INVALID_RAW_REPORT,
                "the Fake EvalScope report is unavailable",
                retryable=False,
            ) from error

    def normalize(
        self,
        compiled: CompiledEvalScopeBenchmark,
        report: EvalScopeRawReport,
    ) -> BenchmarkResult:
        return normalize_evalscope_report(report, compiled, self._profile)

    def _fake_report(self, compiled: CompiledEvalScopeBenchmark) -> EvalScopeRawReport:
        raw_bindings = self._profile.raw_metric_bindings
        requests = compiled.budget_estimate.measurement_requests
        duration = max(1, compiled.budget_estimate.estimated_duration_seconds)
        latency_scale = 0.001 if self._profile.latency_unit is LatencyUnit.SECONDS else 1.0
        metric_values: dict[str, float] = {
            raw_bindings.reliability.submitted: requests,
            raw_bindings.reliability.completed: requests,
            raw_bindings.reliability.failed: 0,
            raw_bindings.reliability.timed_out: 0,
            raw_bindings.reliability.completed_within_window: requests,
            raw_bindings.reliability.scheduled_window_seconds: duration,
            raw_bindings.reliability.measurement_duration_seconds: duration,
            raw_bindings.tokens.successful_input_tokens: requests * compiled.workload.prompt_tokens,
            raw_bindings.tokens.successful_output_tokens: requests
            * compiled.workload.output_tokens,
        }
        percentile_groups = (
            (raw_bindings.latency.e2e, (500, 700, 900)),
            (raw_bindings.latency.ttft, (100, 150, 200)),
            (raw_bindings.latency.tpot, (10, 15, 20)),
            (raw_bindings.latency.itl, (10, 15, 20)),
            (raw_bindings.lengths.input_tokens, (compiled.workload.prompt_tokens,) * 3),
            (raw_bindings.lengths.output_tokens, (compiled.workload.output_tokens,) * 3),
        )
        for index, (bindings, values) in enumerate(percentile_groups):
            scale = latency_scale if index < LATENCY_GROUP_COUNT else 1.0
            metric_values[bindings.p50] = values[0] * scale
            metric_values[bindings.p95] = values[1] * scale
            metric_values[bindings.p99] = values[2] * scale
        metrics = tuple(
            RawMetricSample(name=name, value=value) for name, value in metric_values.items()
        )
        raw_bytes = "\n".join(f"{sample.name}={sample.value}" for sample in metrics).encode("utf-8")
        digest = hashlib.sha256(raw_bytes).hexdigest()
        artifact = ArtifactRef(
            artifact_id=ArtifactId(root=f"artifact_{digest[:32]}"),
            sha256=Sha256Digest(root=f"sha256:{digest}"),
            content_type="application/json",
            size_bytes=len(raw_bytes),
            producer=ArtifactProducer(
                component="evalscope-adapter",
                version=self._profile.adapter_version,
            ),
        )
        return EvalScopeRawReport(
            provider_version=self._profile.provider_version,
            adapter_version=self._profile.adapter_version,
            provider_profile_version=self._profile.profile_version,
            provider_profile_hash=compute_content_hash(self._profile),
            compiled_benchmark_hash=compute_content_hash(compiled),
            traffic_mode=compiled.traffic.mode,
            metrics=metrics,
            oom=False,
            raw_artifact=artifact,
        )
