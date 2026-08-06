# ruff: noqa: TRY003
"""Deterministic fakes for runner contract and workflow tests."""

from __future__ import annotations

from runner.docker import (
    ContainerHandle,
    ContainerSpec,
    ContainerState,
    DockerAdapter,
)
from runner.errors import DockerOperationError, ResourceNotFoundError
from runner.health import (
    GpuMemoryObservation,
    HttpHealthObservation,
    MinimalCompletionObservation,
    ModelListObservation,
    ProcessHealthObservation,
    VllmHealthLayer,
)
from runner.logs import RedactedLogExcerpt, SecretRedactor
from runner.models import (
    CapacityPlannerArtifactsRequest,
    ContainerName,
    InspectEnvironmentRequest,
    JobArtifactsRequest,
    JobStatusRequest,
    OperationResponse,
)


class FakeDockerAdapter(DockerAdapter):
    """In-memory Docker adapter that records typed specifications only."""

    def __init__(self) -> None:
        self.specifications: list[ContainerSpec] = []
        self.handles: dict[str, ContainerHandle] = {}
        self.fail_create = False
        self.fail_start = False
        self.fail_stop = False
        self.fail_remove = False

    def create(self, specification: ContainerSpec) -> ContainerHandle:
        if self.fail_create:
            raise DockerOperationError("fake Docker create failed")
        existing = self.inspect(specification.name)
        if existing is not None:
            return existing
        handle = ContainerHandle(
            container_id=f"container:{specification.name}",
            name=specification.name,
            kind=specification.kind,
            state=ContainerState.CREATED,
        )
        self.specifications.append(specification)
        self.handles[handle.container_id] = handle
        return handle

    def start(self, container_id: str) -> ContainerHandle:
        if self.fail_start:
            raise DockerOperationError("fake Docker start failed")
        handle = self._require(container_id)
        updated = handle.model_copy(update={"state": ContainerState.RUNNING})
        self.handles[container_id] = updated
        return updated

    def stop(self, container_id: str, *, timeout_seconds: int) -> ContainerHandle:
        del timeout_seconds
        if self.fail_stop:
            raise DockerOperationError("fake Docker stop failed")
        handle = self._require(container_id)
        updated = handle.model_copy(update={"state": ContainerState.EXITED})
        self.handles[container_id] = updated
        return updated

    def remove(self, container_id: str) -> None:
        if self.fail_remove:
            raise DockerOperationError("fake Docker remove failed")
        if self.handles.pop(container_id, None) is None:
            raise ResourceNotFoundError("fake container does not exist")

    def inspect(self, name: ContainerName) -> ContainerHandle | None:
        return next((handle for handle in self.handles.values() if handle.name == name), None)

    def list_managed(self) -> tuple[ContainerHandle, ...]:
        return tuple(sorted(self.handles.values(), key=lambda item: item.name))

    def _require(self, container_id: str) -> ContainerHandle:
        try:
            return self.handles[container_id]
        except KeyError as error:
            raise ResourceNotFoundError("fake container does not exist") from error


class FakeNonDockerOperations:
    """Minimal fake for environment and artifact-facing runner actions."""

    def inspect_environment(self, request: InspectEnvironmentRequest) -> OperationResponse:
        return OperationResponse(resource_id=request.plan_id, state="succeeded")

    def get_job_status(self, request: JobStatusRequest) -> OperationResponse:
        return OperationResponse(resource_id=request.payload.job_id, state="running")

    def get_job_artifacts(self, request: JobArtifactsRequest) -> OperationResponse:
        return OperationResponse(resource_id=request.payload.job_id, state="succeeded")

    def get_capacity_planner_artifacts(
        self,
        request: CapacityPlannerArtifactsRequest,
    ) -> OperationResponse:
        return OperationResponse(resource_id=request.payload.job_id, state="succeeded")


class FakeVllmHealthProbe:
    """Typed vLLM probe used without Docker, HTTP, or GPU access."""

    def __init__(self) -> None:
        self.process_observation = ProcessHealthObservation(running=True)
        self.http_observation = HttpHealthObservation(status_code=200)
        self.models_observation = ModelListObservation(model_ids=("Qwen/Qwen3-8B",))
        self.completion_observation = MinimalCompletionObservation(
            succeeded=True,
            served_model_id="Qwen/Qwen3-8B",
            output_text="ok",
            output_tokens=1,
        )
        self.memory_observation = GpuMemoryObservation(
            gpu_index=0,
            used_bytes=1,
            total_bytes=32 * 1024**3,
            expected_process_seen=True,
            foreign_compute_process_count=0,
        )
        self.log_observation = RedactedLogExcerpt(text="startup complete", truncated=False)
        self.calls: list[VllmHealthLayer] = []
        self.requested_completion_model_id: str | None = None

    def set_logs(self, value: bytes | str, *, redactor: SecretRedactor) -> None:
        self.log_observation = redactor.redact(value)

    def process(self, container: ContainerHandle) -> ProcessHealthObservation:
        del container
        self.calls.append(VllmHealthLayer.PROCESS)
        return self.process_observation

    def http_health(self, container: ContainerHandle) -> HttpHealthObservation:
        del container
        self.calls.append(VllmHealthLayer.HTTP)
        return self.http_observation

    def list_models(self, container: ContainerHandle) -> ModelListObservation:
        del container
        self.calls.append(VllmHealthLayer.MODEL_LIST)
        return self.models_observation

    def minimal_completion(
        self,
        container: ContainerHandle,
        *,
        model_id: str,
    ) -> MinimalCompletionObservation:
        del container
        self.calls.append(VllmHealthLayer.MINIMAL_COMPLETION)
        self.requested_completion_model_id = model_id
        return self.completion_observation

    def gpu_memory(self, container: ContainerHandle) -> GpuMemoryObservation:
        del container
        self.calls.append(VllmHealthLayer.GPU_MEMORY)
        return self.memory_observation

    def recent_logs(self, container: ContainerHandle) -> RedactedLogExcerpt:
        del container
        self.calls.append(VllmHealthLayer.FATAL_LOG)
        return self.log_observation
