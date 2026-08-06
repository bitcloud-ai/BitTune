# ruff: noqa: TRY003
"""Deterministic host runner dispatcher and cleanup coordinator."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Protocol, cast

from pydantic import BaseModel

from runner.docker import (
    ContainerHandle,
    ContainerKind,
    ContainerSpec,
    ContainerSpecCompiler,
    ContainerState,
    DockerAdapter,
    MountMode,
    stop_and_remove,
)
from runner.errors import (
    CleanupError,
    DockerOperationError,
    GpuLeaseBusyError,
    IdempotencyConflictError,
    ResourceNotFoundError,
    RunnerRequestInProgressError,
    RunnerServiceError,
)
from runner.health import VllmHealthVerifier
from runner.leases import Clock, GpuLeaseManager, system_utc_now
from runner.models import (
    AcquireGpuLeaseRequest,
    CancelBenchmarkRequest,
    CancelCapacityPlannerRequest,
    CapacityPlannerArtifactsRequest,
    CapacityPlannerStatusRequest,
    CleanupTemporaryRequest,
    ContainerName,
    DeploymentStatusRequest,
    HeartbeatGpuLeaseRequest,
    InspectEnvironmentRequest,
    JobArtifactsRequest,
    JobStatusRequest,
    LeaseResponse,
    OperationResponse,
    ReleaseGpuLeaseRequest,
    RunnerError,
    RunnerRequest,
    RunnerResponse,
    StartBenchmarkRequest,
    StartCapacityPlannerRequest,
    StartDeploymentRequest,
    StopDeploymentRequest,
)


class NonDockerOperations(Protocol):
    """Provider-specific operations implemented in later capability stages."""

    def inspect_environment(self, request: InspectEnvironmentRequest) -> OperationResponse: ...

    def get_job_status(self, request: JobStatusRequest) -> OperationResponse: ...

    def get_job_artifacts(self, request: JobArtifactsRequest) -> OperationResponse: ...

    def get_capacity_planner_artifacts(
        self,
        request: CapacityPlannerArtifactsRequest,
    ) -> OperationResponse: ...


class UnavailableNonDockerOperations:
    """Fail closed until a verified provider adapter is injected."""

    def inspect_environment(self, request: InspectEnvironmentRequest) -> OperationResponse:
        del request
        raise ResourceNotFoundError("environment provider adapter is not configured")

    def get_job_status(self, request: JobStatusRequest) -> OperationResponse:
        del request
        raise ResourceNotFoundError("job provider adapter is not configured")

    def get_job_artifacts(self, request: JobArtifactsRequest) -> OperationResponse:
        del request
        raise ResourceNotFoundError("job artifact adapter is not configured")

    def get_capacity_planner_artifacts(
        self,
        request: CapacityPlannerArtifactsRequest,
    ) -> OperationResponse:
        del request
        raise ResourceNotFoundError("capacity Planner artifact adapter is not configured")


@dataclass(frozen=True, slots=True)
class _DeploymentRuntime:
    handle: ContainerHandle
    lease: LeaseResponse
    worker_id: str
    output_path: Path
    initial_output_bytes: int
    max_disk_growth_bytes: int


@dataclass(frozen=True, slots=True)
class _IdempotencyEntry:
    request_hash: str
    response: RunnerResponse


@dataclass(frozen=True, slots=True)
class _TimedContainerRuntime:
    handle: ContainerHandle
    deadline: datetime
    output_path: Path
    initial_output_bytes: int
    max_disk_growth_bytes: int


MAX_STOP_TIMEOUT_SECONDS = 300


@dataclass(frozen=True, slots=True)
class RunnerServiceConfig:
    container_stop_timeout_seconds: int = 30
    clock: Clock = system_utc_now

    def __post_init__(self) -> None:
        if not 1 <= self.container_stop_timeout_seconds <= MAX_STOP_TIMEOUT_SECONDS:
            raise ValueError("container stop timeout must be between 1 and 300 seconds")


class RunnerService:
    """Validate, deduplicate, execute, and clean up typed runner actions."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        docker: DockerAdapter,
        compiler: ContainerSpecCompiler,
        leases: GpuLeaseManager,
        non_docker: NonDockerOperations | None = None,
        health: VllmHealthVerifier,
        config: RunnerServiceConfig | None = None,
    ) -> None:
        resolved_config = config or RunnerServiceConfig()
        self._roots = compiler.roots
        self._docker = docker
        self._compiler = compiler
        self._leases = leases
        self._non_docker = non_docker or UnavailableNonDockerOperations()
        self._health = health
        self._stop_timeout = resolved_config.container_stop_timeout_seconds
        self._clock = resolved_config.clock
        self._idempotency: dict[str, _IdempotencyEntry] = {}
        self._inflight: dict[str, str] = {}
        self._deployments: dict[str, _DeploymentRuntime] = {}
        self._benchmarks: dict[str, _TimedContainerRuntime] = {}
        self._planner_jobs: dict[str, _TimedContainerRuntime] = {}
        self._mutex = Lock()

    def dispatch(self, request: RunnerRequest) -> RunnerResponse:
        """Dispatch one action and convert classified failures to a typed response."""

        request_hash = self._request_hash(request)
        key = str(request.idempotency_key)
        with self._mutex:
            existing = self._idempotency.get(key)
            if existing is not None:
                if existing.request_hash != request_hash:
                    return self._failure_response(
                        request.request_id,
                        IdempotencyConflictError(
                            "idempotency key was already used with different runner input"
                        ),
                    )
                return existing.response.model_copy(update={"idempotent_replay": True})
            inflight_hash = self._inflight.get(key)
            if inflight_hash is not None:
                if inflight_hash != request_hash:
                    return self._failure_response(
                        request.request_id,
                        IdempotencyConflictError(
                            "idempotency key is executing with different runner input"
                        ),
                    )
                return self._failure_response(
                    request.request_id,
                    RunnerRequestInProgressError("runner request is still executing"),
                )
            self._inflight[key] = request_hash
        response: RunnerResponse | None = None
        try:
            try:
                response = RunnerResponse(
                    request_id=request.request_id,
                    accepted=True,
                    result=self._dispatch_once(request),
                )
            except RunnerServiceError as error:
                response = self._failure_response(request.request_id, error)
        finally:
            with self._mutex:
                self._inflight.pop(key, None)
                if response is not None and response.accepted:
                    self._idempotency[key] = _IdempotencyEntry(request_hash, response)
        if response is None:
            raise AssertionError("runner dispatch returned without a response")
        return response

    def reconcile(
        self,
        *,
        expected_container_names: frozenset[ContainerName],
        reconcilable_container_names: frozenset[ContainerName],
    ) -> tuple[ContainerName, ...]:
        """Clean only persisted-owned containers that are no longer expected."""

        cleaned: list[ContainerName] = []
        for handle in self._docker.list_managed():
            if handle.name in expected_container_names:
                continue
            if handle.name not in reconcilable_container_names:
                continue
            stop_and_remove(self._docker, handle, timeout_seconds=self._stop_timeout)
            self._forget_runtime(handle.name)
            cleaned.append(handle.name)
        cleaned.extend(self.watchdog_maintenance())
        return tuple(sorted(set(cleaned)))

    def watchdog_maintenance(self) -> tuple[ContainerName, ...]:
        """Enforce deadlines, disk budgets, and tracked Lease expiry only."""

        cleaned: list[ContainerName] = []
        cleaned.extend(self._cleanup_expired_timed_containers())
        cleaned.extend(self._cleanup_deployment_disk_budget())
        self._leases.expire_and_cleanup(self._cleanup_expired_lease)
        return tuple(sorted(set(cleaned)))

    def managed_container_names(self) -> frozenset[ContainerName]:
        """Return observed managed names without mutating Docker state."""

        return frozenset(handle.name for handle in self._docker.list_managed())

    def _dispatch_once(self, request: RunnerRequest) -> OperationResponse:
        handlers: tuple[tuple[type[BaseModel], Callable[[BaseModel], OperationResponse]], ...] = (
            (InspectEnvironmentRequest, self._inspect_environment),
            (StartDeploymentRequest, self._start_deployment),
            (StopDeploymentRequest, self._stop_deployment),
            (DeploymentStatusRequest, self._deployment_status),
            (StartBenchmarkRequest, self._start_benchmark),
            (CancelBenchmarkRequest, self._cancel_benchmark),
            (JobStatusRequest, self._job_status),
            (JobArtifactsRequest, self._job_artifacts),
            (StartCapacityPlannerRequest, self._start_capacity_planner),
            (CapacityPlannerStatusRequest, self._capacity_planner_status),
            (CancelCapacityPlannerRequest, self._cancel_capacity_planner),
            (CapacityPlannerArtifactsRequest, self._capacity_planner_artifacts),
            (AcquireGpuLeaseRequest, self._acquire_lease),
            (HeartbeatGpuLeaseRequest, self._heartbeat_lease),
            (ReleaseGpuLeaseRequest, self._release_lease),
            (CleanupTemporaryRequest, self._cleanup_temporary),
        )
        for request_type, handler in handlers:
            if isinstance(request, request_type):
                return handler(request)
        raise AssertionError("runner request union contains an unhandled action")

    def _inspect_environment(self, request: BaseModel) -> OperationResponse:
        return self._non_docker.inspect_environment(cast(InspectEnvironmentRequest, request))

    def _start_deployment(self, request: BaseModel) -> OperationResponse:
        typed = cast(StartDeploymentRequest, request)
        payload = typed.payload
        self._leases.expire_and_cleanup(self._cleanup_expired_lease)
        self._assert_no_unclaimed_vllm_container()
        lease_id = f"gpu-lease-{payload.deployment_id.removeprefix('deployment_')}"
        lease_result = self._leases.acquire(
            lease_id=lease_id,
            owner_id=payload.worker_id,
            idempotency_key=typed.idempotency_key,
            duration=timedelta(seconds=payload.task_timeout_seconds),
        )
        handle: ContainerHandle | None = None
        try:
            specification = self._compiler.deployment(payload)
            output_path = self._writable_output(specification)
            initial_output_bytes = self._initial_output_size(output_path)
            handle = self._docker.inspect(specification.name)
            if handle is None:
                handle = self._docker.create(specification)
            if handle.kind is not specification.kind:
                raise DockerOperationError("existing managed container kind does not match")
            if handle.state is not ContainerState.RUNNING:
                handle = self._docker.start(handle.container_id)
            self._health.assert_healthy(
                handle,
                expected_model_repository=payload.model_repository,
            )
            self._deployments[payload.deployment_id] = _DeploymentRuntime(
                handle=handle,
                lease=lease_result.lease,
                worker_id=payload.worker_id,
                output_path=output_path,
                initial_output_bytes=initial_output_bytes,
                max_disk_growth_bytes=payload.max_disk_growth_bytes,
            )
            return OperationResponse(
                resource_id=payload.deployment_id,
                state="running",
                lease=lease_result.lease,
            )
        except RunnerServiceError:
            try:
                if handle is not None:
                    self._cleanup_handle(handle)
            finally:
                self._release_after_failed_start(lease_result.lease)
            raise

    def _stop_deployment(self, request: BaseModel) -> OperationResponse:
        typed = cast(StopDeploymentRequest, request)
        deployment_id = typed.payload.deployment_id
        runtime = self._deployments.get(deployment_id)
        handle = None if runtime is None else runtime.handle
        if handle is None:
            handle = self._docker.inspect(self._compiler.deployment_name(deployment_id))
        if handle is not None:
            stop_and_remove(self._docker, handle, timeout_seconds=self._stop_timeout)
        if runtime is not None:
            self._leases.release(
                lease_id=runtime.lease.lease_id,
                owner_id=runtime.worker_id,
                fencing_token=runtime.lease.fencing_token,
            )
            self._deployments.pop(deployment_id, None)
        return OperationResponse(resource_id=deployment_id, state="stopped")

    def _deployment_status(self, request: BaseModel) -> OperationResponse:
        typed = cast(DeploymentStatusRequest, request)
        deployment_id = typed.payload.deployment_id
        handle = self._docker.inspect(self._compiler.deployment_name(deployment_id))
        if handle is None:
            raise ResourceNotFoundError("deployment container does not exist")
        state = "running" if handle.state is ContainerState.RUNNING else "stopped"
        runtime = self._deployments.get(deployment_id)
        return OperationResponse(
            resource_id=deployment_id,
            state=state,
            lease=None if runtime is None else runtime.lease,
        )

    def _start_benchmark(self, request: BaseModel) -> OperationResponse:
        typed = cast(StartBenchmarkRequest, request)
        payload = typed.payload
        specification = self._compiler.benchmark(payload)
        output_path = self._writable_output(specification)
        initial_output_bytes = self._initial_output_size(output_path)
        handle = self._docker.inspect(specification.name)
        try:
            if handle is None:
                handle = self._docker.create(specification)
            if handle.kind is not specification.kind:
                raise DockerOperationError("existing managed container kind does not match")
            if handle.state is not ContainerState.RUNNING:
                handle = self._docker.start(handle.container_id)
            self._benchmarks[payload.benchmark_id] = _TimedContainerRuntime(
                handle=handle,
                deadline=self._now() + timedelta(seconds=payload.max_duration_seconds),
                output_path=output_path,
                initial_output_bytes=initial_output_bytes,
                max_disk_growth_bytes=payload.max_disk_growth_bytes,
            )
            return OperationResponse(resource_id=payload.benchmark_id, state="running")
        except RunnerServiceError:
            if handle is not None:
                self._cleanup_handle(handle)
            raise

    def _cancel_benchmark(self, request: BaseModel) -> OperationResponse:
        typed = cast(CancelBenchmarkRequest, request)
        job_id = typed.payload.job_id
        benchmark_id = job_id.replace("job_", "benchmark_", 1)
        runtime = self._benchmarks.pop(benchmark_id, None)
        handle = None if runtime is None else runtime.handle
        if handle is None:
            handle = self._docker.inspect(self._compiler.benchmark_name(benchmark_id))
        if handle is not None:
            stop_and_remove(self._docker, handle, timeout_seconds=self._stop_timeout)
        return OperationResponse(resource_id=job_id, state="cancelled")

    def _job_status(self, request: BaseModel) -> OperationResponse:
        return self._non_docker.get_job_status(cast(JobStatusRequest, request))

    def _job_artifacts(self, request: BaseModel) -> OperationResponse:
        return self._non_docker.get_job_artifacts(cast(JobArtifactsRequest, request))

    def _start_capacity_planner(self, request: BaseModel) -> OperationResponse:
        typed = cast(StartCapacityPlannerRequest, request)
        payload = typed.payload
        specification = self._compiler.planner(payload)
        output_path = self._writable_output(specification)
        initial_output_bytes = self._initial_output_size(output_path)
        handle = self._docker.inspect(specification.name)
        try:
            if handle is None:
                handle = self._docker.create(specification)
            if handle.kind is not specification.kind:
                raise DockerOperationError("existing managed container kind does not match")
            if handle.state is not ContainerState.RUNNING:
                handle = self._docker.start(handle.container_id)
            self._planner_jobs[payload.job_id] = _TimedContainerRuntime(
                handle=handle,
                deadline=self._now() + timedelta(seconds=payload.budget.max_duration_seconds),
                output_path=output_path,
                initial_output_bytes=initial_output_bytes,
                max_disk_growth_bytes=payload.budget.max_disk_growth_bytes,
            )
            return OperationResponse(resource_id=payload.job_id, state="running")
        except RunnerServiceError:
            if handle is not None:
                self._cleanup_handle(handle)
            raise

    def _capacity_planner_status(self, request: BaseModel) -> OperationResponse:
        typed = cast(CapacityPlannerStatusRequest, request)
        job_id = typed.payload.job_id
        handle = self._docker.inspect(self._compiler.planner_name(job_id))
        if handle is None:
            raise ResourceNotFoundError("capacity Planner container does not exist")
        state = "running" if handle.state is ContainerState.RUNNING else "succeeded"
        return OperationResponse(resource_id=job_id, state=state)

    def _cancel_capacity_planner(self, request: BaseModel) -> OperationResponse:
        typed = cast(CancelCapacityPlannerRequest, request)
        job_id = typed.payload.job_id
        runtime = self._planner_jobs.pop(job_id, None)
        handle = None if runtime is None else runtime.handle
        if handle is None:
            handle = self._docker.inspect(self._compiler.planner_name(job_id))
        if handle is not None:
            stop_and_remove(self._docker, handle, timeout_seconds=self._stop_timeout)
        return OperationResponse(resource_id=job_id, state="cancelled")

    def _capacity_planner_artifacts(self, request: BaseModel) -> OperationResponse:
        return self._non_docker.get_capacity_planner_artifacts(
            cast(CapacityPlannerArtifactsRequest, request)
        )

    def _acquire_lease(self, request: BaseModel) -> OperationResponse:
        typed = cast(AcquireGpuLeaseRequest, request)
        payload = typed.payload
        result = self._leases.acquire(
            lease_id=payload.lease_id,
            owner_id=payload.owner_id,
            idempotency_key=typed.idempotency_key,
            duration=timedelta(seconds=payload.lease_duration_seconds),
        )
        return OperationResponse(
            resource_id=payload.lease_id,
            state="accepted",
            lease=result.lease,
        )

    def _heartbeat_lease(self, request: BaseModel) -> OperationResponse:
        typed = cast(HeartbeatGpuLeaseRequest, request)
        payload = typed.payload
        if payload.fencing_token is None:
            raise IdempotencyConflictError("heartbeat requires the current fencing token")
        lease = self._leases.heartbeat(
            lease_id=payload.lease_id,
            owner_id=payload.owner_id,
            fencing_token=payload.fencing_token,
            duration=timedelta(seconds=payload.lease_duration_seconds),
        )
        return OperationResponse(resource_id=payload.lease_id, state="running", lease=lease)

    def _release_lease(self, request: BaseModel) -> OperationResponse:
        typed = cast(ReleaseGpuLeaseRequest, request)
        payload = typed.payload
        if payload.fencing_token is None:
            raise IdempotencyConflictError("release requires the current fencing token")
        self._leases.release(
            lease_id=payload.lease_id,
            owner_id=payload.owner_id,
            fencing_token=payload.fencing_token,
        )
        return OperationResponse(resource_id=payload.lease_id, state="stopped")

    def _cleanup_temporary(self, request: BaseModel) -> OperationResponse:
        typed = cast(CleanupTemporaryRequest, request)
        target = self._roots.resolve(
            typed.payload.temporary_ref,
            must_exist=False,
            require_directory=None,
        )
        if target.exists():
            if not target.is_dir() or target.is_symlink():
                raise CleanupError("temporary cleanup target must be a real directory")
            try:
                shutil.rmtree(target)
            except OSError as error:
                raise CleanupError("temporary cleanup failed") from error
        return OperationResponse(
            resource_id=str(typed.payload.temporary_ref.relative_path), state="succeeded"
        )

    def _cleanup_expired_lease(self, lease: LeaseResponse) -> None:
        for deployment_id, runtime in tuple(self._deployments.items()):
            if runtime.lease.lease_id != lease.lease_id:
                continue
            stop_and_remove(
                self._docker,
                runtime.handle,
                timeout_seconds=self._stop_timeout,
            )
            self._deployments.pop(deployment_id, None)

    def _assert_no_unclaimed_vllm_container(self) -> None:
        tracked_ids = {runtime.handle.container_id for runtime in self._deployments.values()}
        for handle in self._docker.list_managed():
            if handle.kind is ContainerKind.VLLM and handle.container_id not in tracked_ids:
                raise GpuLeaseBusyError(
                    "unclaimed managed vLLM container requires authoritative reconciliation"
                )

    def _cleanup_expired_timed_containers(self) -> list[ContainerName]:
        now = self._now()
        cleaned: list[ContainerName] = []
        runtime_groups = (self._benchmarks, self._planner_jobs)
        for runtimes in runtime_groups:
            for resource_id, runtime in tuple(runtimes.items()):
                if runtime.deadline > now and not self._disk_growth_exceeded(runtime):
                    continue
                stop_and_remove(
                    self._docker,
                    runtime.handle,
                    timeout_seconds=self._stop_timeout,
                )
                runtimes.pop(resource_id, None)
                cleaned.append(runtime.handle.name)
        return cleaned

    def _cleanup_deployment_disk_budget(self) -> list[ContainerName]:
        cleaned: list[ContainerName] = []
        for deployment_id, runtime in tuple(self._deployments.items()):
            if not self._disk_growth_exceeded(runtime):
                continue
            stop_and_remove(self._docker, runtime.handle, timeout_seconds=self._stop_timeout)
            with suppress(RunnerServiceError):
                self._leases.release(
                    lease_id=runtime.lease.lease_id,
                    owner_id=runtime.worker_id,
                    fencing_token=runtime.lease.fencing_token,
                )
            self._deployments.pop(deployment_id, None)
            cleaned.append(runtime.handle.name)
        return cleaned

    def _forget_runtime(self, container_name: str) -> None:
        for deployment_id, runtime in tuple(self._deployments.items()):
            if runtime.handle.name != container_name:
                continue
            with suppress(RunnerServiceError):
                self._leases.release(
                    lease_id=runtime.lease.lease_id,
                    owner_id=runtime.worker_id,
                    fencing_token=runtime.lease.fencing_token,
                )
            self._deployments.pop(deployment_id, None)
        for runtimes in (self._benchmarks, self._planner_jobs):
            for resource_id, timed_runtime in tuple(runtimes.items()):
                if timed_runtime.handle.name == container_name:
                    runtimes.pop(resource_id, None)

    @staticmethod
    def _writable_output(specification: ContainerSpec) -> Path:
        outputs = tuple(
            mount.host_location
            for mount in specification.mounts
            if mount.mode is MountMode.READ_WRITE
        )
        if len(outputs) != 1:
            raise DockerOperationError("managed container requires exactly one writable output")
        return outputs[0]

    @classmethod
    def _disk_growth_exceeded(
        cls,
        runtime: _DeploymentRuntime | _TimedContainerRuntime,
    ) -> bool:
        try:
            current = cls._directory_size_bytes(runtime.output_path)
        except OSError:
            return True
        return current - runtime.initial_output_bytes > runtime.max_disk_growth_bytes

    @classmethod
    def _initial_output_size(cls, output_path: Path) -> int:
        try:
            return cls._directory_size_bytes(output_path)
        except OSError as error:
            raise CleanupError("managed output directory cannot be inspected") from error

    @staticmethod
    def _directory_size_bytes(root: Path) -> int:
        total = 0
        pending = [root]
        while pending:
            current = pending.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        raise OSError("writable output contains a symbolic link")
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
        return total

    def _now(self) -> datetime:
        current = self._clock()
        if current.tzinfo is None:
            raise ValueError("runner clock must return a timezone-aware datetime")
        return current.astimezone(UTC)

    def _cleanup_handle(self, handle: ContainerHandle) -> None:
        stop_and_remove(self._docker, handle, timeout_seconds=self._stop_timeout)

    def _release_after_failed_start(self, lease: LeaseResponse) -> None:
        # The original classified start failure remains primary.  A later
        # reconciliation pass will observe and expire a stale lease.
        with suppress(RunnerServiceError):
            self._leases.release(
                lease_id=lease.lease_id,
                owner_id=lease.owner_id,
                fencing_token=lease.fencing_token,
            )

    @staticmethod
    def _request_hash(request: RunnerRequest) -> str:
        serialized = json.dumps(
            request.model_dump(mode="json"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    @staticmethod
    def _failure_response(request_id: str, error: RunnerServiceError) -> RunnerResponse:
        return RunnerResponse(
            request_id=request_id,
            accepted=False,
            error=RunnerError(
                code=error.code,
                message=str(error),
                retryable=error.retryable,
            ),
        )
