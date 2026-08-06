"""Benchmark Provider ports implemented by the pinned EvalScope adapter."""

from typing import Protocol

from autopilot.capabilities.benchmark.domain.models import (
    BenchmarkResult,
    CompiledEvalScopeBenchmark,
)
from autopilot.capabilities.benchmark.ports.lifecycle import (
    BenchmarkAdapterCapabilities,
    BenchmarkOperation,
    BenchmarkStartContext,
    RunnerArtifactLocation,
)
from autopilot.capabilities.benchmark.ports.models import EvalScopeRawReport
from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.identifiers import JobId


class BenchmarkArtifactLocator(Protocol):
    def locate_for_runner(self, artifact: ArtifactRef) -> RunnerArtifactLocation: ...


class BenchmarkReportReader(Protocol):
    def read_evalscope_report(self, job_id: JobId) -> EvalScopeRawReport: ...


class BenchmarkAdapter(Protocol):
    def capabilities(self) -> BenchmarkAdapterCapabilities: ...

    def validate(self, compiled: CompiledEvalScopeBenchmark) -> None: ...

    def start(
        self,
        compiled: CompiledEvalScopeBenchmark,
        context: BenchmarkStartContext,
    ) -> BenchmarkOperation: ...

    def status(self, context: BenchmarkStartContext) -> BenchmarkOperation: ...

    def cancel(self, context: BenchmarkStartContext) -> BenchmarkOperation: ...

    def collect(self, context: BenchmarkStartContext) -> EvalScopeRawReport: ...

    def normalize(
        self,
        compiled: CompiledEvalScopeBenchmark,
        report: EvalScopeRawReport,
    ) -> BenchmarkResult: ...
