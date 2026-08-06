"""Fixed, layered vLLM startup-health boundary for GPU deployments."""

# Validation messages are part of the typed Runner failure boundary.
# ruff: noqa: TRY003

from __future__ import annotations

import re
from enum import StrEnum
from typing import Protocol, Self

from pydantic import Field, model_validator

from runner.docker import ContainerHandle
from runner.errors import VllmHealthCheckError, VllmHealthProbeUnavailableError
from runner.logs import RedactedLogExcerpt
from runner.models import RepositoryId, RunnerModel


class VllmHealthLayer(StrEnum):
    PROCESS = "process"
    HTTP = "http"
    MODEL_LIST = "model_list"
    MINIMAL_COMPLETION = "minimal_completion"
    GPU_MEMORY = "gpu_memory"
    FATAL_LOG = "fatal_log"


class VllmHealthFailure(StrEnum):
    PROCESS_NOT_RUNNING = "process_not_running"
    HTTP_NOT_READY = "http_not_ready"
    MODEL_LIST_EMPTY = "model_list_empty"
    EXPECTED_MODEL_MISSING = "expected_model_missing"
    COMPLETION_FAILED = "completion_failed"
    COMPLETION_EMPTY = "completion_empty"
    GPU_MEMORY_INVALID = "gpu_memory_invalid"
    GPU_NOT_EXCLUSIVE = "gpu_not_exclusive"
    FATAL_LOG_DETECTED = "fatal_log_detected"


class ProcessHealthObservation(RunnerModel):
    running: bool


class HttpHealthObservation(RunnerModel):
    status_code: int = Field(ge=100, le=599)


class ModelListObservation(RunnerModel):
    model_ids: tuple[str, ...] = Field(max_length=32)


class MinimalCompletionObservation(RunnerModel):
    succeeded: bool
    served_model_id: str = Field(min_length=1, max_length=512)
    output_text: str = Field(max_length=4_096)
    output_tokens: int = Field(ge=0, le=256)


class GpuMemoryObservation(RunnerModel):
    gpu_index: int = Field(ge=0, le=0)
    used_bytes: int = Field(ge=0, le=1_000_000_000_000)
    total_bytes: int = Field(ge=1, le=1_000_000_000_000)
    expected_process_seen: bool
    foreign_compute_process_count: int = Field(ge=0, le=65_536)


class VllmHealthCheck(RunnerModel):
    layer: VllmHealthLayer
    passed: bool
    failure: VllmHealthFailure | None = None

    @model_validator(mode="after")
    def validate_failure(self) -> Self:
        if self.passed == (self.failure is not None):
            raise ValueError("passed checks cannot have a failure and failed checks require one")
        return self


class VllmHealthResult(RunnerModel):
    healthy: bool
    checks: tuple[VllmHealthCheck, ...] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def validate_health(self) -> Self:
        if self.healthy != all(check.passed for check in self.checks):
            raise ValueError("health result must match its check outcomes")
        return self


class VllmHealthProbe(Protocol):
    """Narrow provider port with fixed checks and no command or exec surface."""

    def process(self, container: ContainerHandle) -> ProcessHealthObservation: ...

    def http_health(self, container: ContainerHandle) -> HttpHealthObservation: ...

    def list_models(self, container: ContainerHandle) -> ModelListObservation: ...

    def minimal_completion(
        self,
        container: ContainerHandle,
        *,
        model_id: str,
    ) -> MinimalCompletionObservation: ...

    def gpu_memory(self, container: ContainerHandle) -> GpuMemoryObservation: ...

    def recent_logs(self, container: ContainerHandle) -> RedactedLogExcerpt: ...


class UnavailableVllmHealthProbe:
    """Production default until a G0-verified fixed transport is configured."""

    @staticmethod
    def _unavailable() -> None:
        raise VllmHealthProbeUnavailableError("verified vLLM health probe is not configured")

    def process(self, container: ContainerHandle) -> ProcessHealthObservation:
        del container
        self._unavailable()
        raise AssertionError("unreachable")

    def http_health(self, container: ContainerHandle) -> HttpHealthObservation:
        del container
        self._unavailable()
        raise AssertionError("unreachable")

    def list_models(self, container: ContainerHandle) -> ModelListObservation:
        del container
        self._unavailable()
        raise AssertionError("unreachable")

    def minimal_completion(
        self,
        container: ContainerHandle,
        *,
        model_id: str,
    ) -> MinimalCompletionObservation:
        del container, model_id
        self._unavailable()
        raise AssertionError("unreachable")

    def gpu_memory(self, container: ContainerHandle) -> GpuMemoryObservation:
        del container
        self._unavailable()
        raise AssertionError("unreachable")

    def recent_logs(self, container: ContainerHandle) -> RedactedLogExcerpt:
        del container
        self._unavailable()
        raise AssertionError("unreachable")


_FATAL_LOG_PATTERNS = (
    re.compile(r"\bfatal\b", re.IGNORECASE),
    re.compile(r"\btraceback \(most recent call last\)", re.IGNORECASE),
    re.compile(r"\bcuda out of memory\b", re.IGNORECASE),
    re.compile(r"\bengine core (?:initialization )?failed\b", re.IGNORECASE),
    re.compile(r"\bsegmentation fault\b", re.IGNORECASE),
    re.compile(r"\buncaught exception\b", re.IGNORECASE),
)
_HTTP_SUCCESS_MIN = 200
_HTTP_SUCCESS_MAX_EXCLUSIVE = 300


class VllmHealthVerifier:
    """Evaluate the fixed startup checks without accepting provider options."""

    def __init__(self, probe: VllmHealthProbe) -> None:
        self._probe = probe

    def verify(  # noqa: PLR0911
        self,
        container: ContainerHandle,
        *,
        expected_model_repository: RepositoryId,
    ) -> VllmHealthResult:
        checks: list[VllmHealthCheck] = []
        process = self._probe.process(container)
        if not process.running:
            return self._failed(
                checks,
                VllmHealthLayer.PROCESS,
                VllmHealthFailure.PROCESS_NOT_RUNNING,
            )
        checks.append(VllmHealthCheck(layer=VllmHealthLayer.PROCESS, passed=True))

        http = self._probe.http_health(container)
        if not _HTTP_SUCCESS_MIN <= http.status_code < _HTTP_SUCCESS_MAX_EXCLUSIVE:
            return self._failed(checks, VllmHealthLayer.HTTP, VllmHealthFailure.HTTP_NOT_READY)
        checks.append(VllmHealthCheck(layer=VllmHealthLayer.HTTP, passed=True))

        accepted_model_ids = (
            str(expected_model_repository),
            f"/models/{expected_model_repository}",
        )
        models = self._probe.list_models(container)
        if not models.model_ids:
            return self._failed(
                checks,
                VllmHealthLayer.MODEL_LIST,
                VllmHealthFailure.MODEL_LIST_EMPTY,
            )
        if not any(model_id in accepted_model_ids for model_id in models.model_ids):
            return self._failed(
                checks,
                VllmHealthLayer.MODEL_LIST,
                VllmHealthFailure.EXPECTED_MODEL_MISSING,
            )
        checks.append(VllmHealthCheck(layer=VllmHealthLayer.MODEL_LIST, passed=True))

        completion_model_id = next(
            model_id for model_id in models.model_ids if model_id in accepted_model_ids
        )
        completion = self._probe.minimal_completion(
            container,
            model_id=completion_model_id,
        )
        if not completion.succeeded or completion.served_model_id not in accepted_model_ids:
            return self._failed(
                checks,
                VllmHealthLayer.MINIMAL_COMPLETION,
                VllmHealthFailure.COMPLETION_FAILED,
            )
        if not completion.output_text.strip() or completion.output_tokens < 1:
            return self._failed(
                checks,
                VllmHealthLayer.MINIMAL_COMPLETION,
                VllmHealthFailure.COMPLETION_EMPTY,
            )
        checks.append(VllmHealthCheck(layer=VllmHealthLayer.MINIMAL_COMPLETION, passed=True))

        memory = self._probe.gpu_memory(container)
        if memory.used_bytes < 1 or memory.used_bytes > memory.total_bytes:
            return self._failed(
                checks,
                VllmHealthLayer.GPU_MEMORY,
                VllmHealthFailure.GPU_MEMORY_INVALID,
            )
        if not memory.expected_process_seen or memory.foreign_compute_process_count != 0:
            return self._failed(
                checks,
                VllmHealthLayer.GPU_MEMORY,
                VllmHealthFailure.GPU_NOT_EXCLUSIVE,
            )
        checks.append(VllmHealthCheck(layer=VllmHealthLayer.GPU_MEMORY, passed=True))

        logs = self._probe.recent_logs(container)
        if any(pattern.search(logs.text) is not None for pattern in _FATAL_LOG_PATTERNS):
            return self._failed(
                checks,
                VllmHealthLayer.FATAL_LOG,
                VllmHealthFailure.FATAL_LOG_DETECTED,
            )
        checks.append(VllmHealthCheck(layer=VllmHealthLayer.FATAL_LOG, passed=True))
        return VllmHealthResult(healthy=True, checks=tuple(checks))

    def assert_healthy(
        self,
        container: ContainerHandle,
        *,
        expected_model_repository: RepositoryId,
    ) -> VllmHealthResult:
        result = self.verify(
            container,
            expected_model_repository=expected_model_repository,
        )
        if result.healthy:
            return result
        failure = next(check.failure for check in result.checks if not check.passed)
        raise VllmHealthCheckError(f"vLLM startup health failed: {failure}")

    @staticmethod
    def _failed(
        passed_checks: list[VllmHealthCheck],
        layer: VllmHealthLayer,
        failure: VllmHealthFailure,
    ) -> VllmHealthResult:
        return VllmHealthResult(
            healthy=False,
            checks=(*passed_checks, VllmHealthCheck(layer=layer, passed=False, failure=failure)),
        )
