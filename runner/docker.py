# ruff: noqa: TRY003
"""Typed Docker adapter and fixed container specification compiler."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol, Self

from pydantic import Field, model_validator

from runner.errors import (
    DockerOperationError,
    ImageNotAllowedError,
    PathBoundaryError,
    ResourceNotFoundError,
)
from runner.models import (
    BenchmarkStartPayload,
    CapacityPlannerStartPayload,
    ContainerName,
    DeploymentStartPayload,
    HuggingFacePlannerModelRef,
    ImageDigest,
    RunnerArtifactInput,
    RunnerModel,
    SecretRef,
    StorageRef,
    StorageRoot,
)
from runner.paths import RootRegistry


class ContainerKind(StrEnum):
    VLLM = "vllm"
    EVALSCOPE = "evalscope"
    PLANNER = "planner"
    CUDA_PROBE = "cuda-probe"


class EntrypointProfile(StrEnum):
    VLLM_OPENAI_SERVER = "vllm-openai-server"
    EVALSCOPE_PROVIDER_RUNTIME = "evalscope-provider-runtime"
    PLANNER_PROVIDER_RUNTIME = "planner-provider-runtime"


class ContainerState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    EXITED = "exited"
    REMOVED = "removed"
    FAILED = "failed"


class MountMode(StrEnum):
    READ_ONLY = "ro"
    READ_WRITE = "rw"


class ContainerMount(RunnerModel):
    """An internal mount whose host side was resolved by ``RootRegistry``."""

    host_root: StorageRoot
    host_location: Path
    container_location: str = Field(pattern=r"^/[A-Za-z0-9_./-]+$", max_length=128)
    mode: MountMode


class CredentialBinding(RunnerModel):
    """Map an allowlisted container variable to a logical systemd credential."""

    variable: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    secret_ref: SecretRef


class VllmProviderArguments(RunnerModel):
    tensor_parallel_size: int
    max_model_len: int
    gpu_memory_utilization: float
    max_num_seqs: int
    max_num_batched_tokens: int
    enable_chunked_prefill: bool
    trust_remote_code: bool
    model_repository: str
    model_revision: str


class PlannerProviderArguments(RunnerModel):
    model_source: Literal["huggingface", "artifact"]
    model_repository: str | None = None
    model_revision: str
    tensor_parallel_size: Literal[1] = 1


class ContainerResourceLimits(RunnerModel):
    cpu_millis: int | None = Field(default=None, ge=100, le=32_000)
    memory_bytes: int | None = Field(default=None, ge=268_435_456, le=68_719_476_736)
    max_disk_growth_bytes: int = Field(ge=1, le=20_000_000_000)


class ContainerSpec(RunnerModel):
    """Closed internal Docker configuration; callers cannot construct host paths."""

    kind: ContainerKind
    name: ContainerName
    image: ImageDigest
    entrypoint_profile: EntrypointProfile
    network: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_.-]+$")
    gpu_index: int | None = Field(default=None, ge=0, le=0)
    exclusive_gpu: bool
    pid_limit: int = Field(ge=16, le=65_536)
    task_timeout_seconds: int = Field(ge=1, le=1_800)
    resource_limits: ContainerResourceLimits
    mounts: tuple[ContainerMount, ...] = Field(max_length=4)
    credentials: tuple[CredentialBinding, ...] = Field(max_length=4)
    vllm_arguments: VllmProviderArguments | None = None
    planner_arguments: PlannerProviderArguments | None = None
    labels: tuple[tuple[str, str], ...] = Field(max_length=8)

    @model_validator(mode="after")
    def validate_kind_contract(self) -> Self:
        if self.kind is ContainerKind.VLLM:
            if (
                self.gpu_index != 0
                or not self.exclusive_gpu
                or self.vllm_arguments is None
                or self.planner_arguments is not None
            ):
                raise ValueError("vLLM container does not match the fixed GPU contract")
        elif self.gpu_index is not None or self.exclusive_gpu or self.vllm_arguments is not None:
            raise ValueError("non-vLLM containers cannot use the measured GPU or vLLM arguments")
        if self.kind is ContainerKind.PLANNER:
            limits = self.resource_limits
            if (
                self.planner_arguments is None
                or limits.cpu_millis is None
                or limits.memory_bytes is None
            ):
                raise ValueError("Planner containers require typed arguments and resource limits")
        elif self.planner_arguments is not None:
            raise ValueError("Planner arguments are restricted to Planner containers")
        return self


class ContainerHandle(RunnerModel):
    container_id: str = Field(min_length=12, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    name: ContainerName
    kind: ContainerKind
    state: ContainerState


class DockerAdapter(Protocol):
    """Narrow Docker SDK port.  It intentionally has no generic execution method."""

    def create(self, specification: ContainerSpec) -> ContainerHandle: ...

    def start(self, container_id: str) -> ContainerHandle: ...

    def stop(self, container_id: str, *, timeout_seconds: int) -> ContainerHandle: ...

    def remove(self, container_id: str) -> None: ...

    def inspect(self, name: ContainerName) -> ContainerHandle | None: ...

    def list_managed(self) -> tuple[ContainerHandle, ...]: ...


class RunnerDockerPolicy(RunnerModel):
    """Trusted deployment-time Docker allowlist."""

    network: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_.-]+$")
    allowed_images: Mapping[ContainerKind, frozenset[ImageDigest]]
    evalscope_image: ImageDigest
    planner_image: ImageDigest | None = None

    def assert_image(self, kind: ContainerKind, image: ImageDigest) -> None:
        if image not in self.allowed_images.get(kind, frozenset()):
            raise ImageNotAllowedError(f"image digest is not allowed for {kind.value}")


class ContainerSpecCompiler:
    """Compile validated runner payloads into fixed Docker specifications."""

    def __init__(self, *, roots: RootRegistry, policy: RunnerDockerPolicy) -> None:
        self._roots = roots
        self._policy = policy

    @property
    def roots(self) -> RootRegistry:
        """Expose the already-validated root registry to the coordinator."""

        return self._roots

    def deployment(self, payload: DeploymentStartPayload) -> ContainerSpec:
        self._policy.assert_image(ContainerKind.VLLM, payload.image)
        model_cache = self._roots.prepare_directory(payload.model_cache)
        output_ref = StorageRef.model_validate(
            {
                "root": StorageRoot.OUTPUT,
                "relative_path": f"deployments/{payload.deployment_id}",
            }
        )
        output = self._roots.prepare_directory(output_ref)
        credentials: tuple[CredentialBinding, ...] = ()
        if payload.model_token is not None:
            credentials = (CredentialBinding(variable="HF_TOKEN", secret_ref=payload.model_token),)
        parameters = payload.parameters
        return ContainerSpec(
            kind=ContainerKind.VLLM,
            name=self.deployment_name(payload.deployment_id),
            image=payload.image,
            entrypoint_profile=EntrypointProfile.VLLM_OPENAI_SERVER,
            network=self._policy.network,
            gpu_index=0,
            exclusive_gpu=True,
            pid_limit=payload.pid_limit,
            task_timeout_seconds=payload.task_timeout_seconds,
            resource_limits=ContainerResourceLimits(
                max_disk_growth_bytes=payload.max_disk_growth_bytes,
            ),
            mounts=(
                ContainerMount(
                    host_root=StorageRoot.MODEL_CACHE,
                    host_location=model_cache,
                    container_location="/models",
                    mode=MountMode.READ_ONLY,
                ),
                ContainerMount(
                    host_root=StorageRoot.OUTPUT,
                    host_location=output,
                    container_location="/output",
                    mode=MountMode.READ_WRITE,
                ),
            ),
            credentials=credentials,
            vllm_arguments=VllmProviderArguments(
                tensor_parallel_size=parameters.tensor_parallel_size,
                max_model_len=parameters.max_model_len,
                gpu_memory_utilization=parameters.gpu_memory_utilization,
                max_num_seqs=parameters.max_num_seqs,
                max_num_batched_tokens=parameters.max_num_batched_tokens,
                enable_chunked_prefill=parameters.enable_chunked_prefill,
                trust_remote_code=parameters.trust_remote_code,
                model_repository=payload.model_repository,
                model_revision=str(payload.model_revision),
            ),
            labels=(
                ("autopilot.managed", "true"),
                ("autopilot.kind", ContainerKind.VLLM.value),
                ("autopilot.resource", payload.deployment_id),
            ),
        )

    def benchmark(self, payload: BenchmarkStartPayload) -> ContainerSpec:
        image = self._policy.evalscope_image
        self._policy.assert_image(ContainerKind.EVALSCOPE, image)
        compiled_spec = self._resolve_artifact(payload.compiled_spec_artifact)
        output_ref = StorageRef.model_validate(
            {
                "root": StorageRoot.OUTPUT,
                "relative_path": f"benchmarks/{payload.benchmark_id}",
            }
        )
        output = self._roots.prepare_directory(output_ref)
        return ContainerSpec(
            kind=ContainerKind.EVALSCOPE,
            name=self.benchmark_name(payload.benchmark_id),
            image=image,
            entrypoint_profile=EntrypointProfile.EVALSCOPE_PROVIDER_RUNTIME,
            network=self._policy.network,
            gpu_index=None,
            exclusive_gpu=False,
            pid_limit=payload.pid_limit,
            task_timeout_seconds=payload.max_duration_seconds,
            resource_limits=ContainerResourceLimits(
                cpu_millis=payload.cpu_millis,
                memory_bytes=payload.max_memory_bytes,
                max_disk_growth_bytes=payload.max_disk_growth_bytes,
            ),
            mounts=(
                ContainerMount(
                    host_root=StorageRoot.OUTPUT,
                    host_location=compiled_spec,
                    container_location="/input/compiled-spec.json",
                    mode=MountMode.READ_ONLY,
                ),
                ContainerMount(
                    host_root=StorageRoot.OUTPUT,
                    host_location=output,
                    container_location="/output",
                    mode=MountMode.READ_WRITE,
                ),
            ),
            credentials=(),
            labels=(
                ("autopilot.managed", "true"),
                ("autopilot.kind", ContainerKind.EVALSCOPE.value),
                ("autopilot.resource", payload.benchmark_id),
            ),
        )

    def planner(self, payload: CapacityPlannerStartPayload) -> ContainerSpec:
        image = self._policy.planner_image
        if image is None:
            raise ImageNotAllowedError("Planner image Digest is not configured")
        self._policy.assert_image(ContainerKind.PLANNER, image)
        hardware = self._resolve_artifact(payload.hardware_passport_artifact)
        model_config = self._resolve_artifact(payload.model_ref.config_artifact)
        output_ref = StorageRef.model_validate(
            {
                "root": StorageRoot.OUTPUT,
                "relative_path": f"planner/{payload.job_id}",
            }
        )
        output = self._roots.prepare_directory(output_ref)
        mounts = [
            ContainerMount(
                host_root=StorageRoot.OUTPUT,
                host_location=model_config,
                container_location="/input/model-config.json",
                mode=MountMode.READ_ONLY,
            ),
            ContainerMount(
                host_root=StorageRoot.OUTPUT,
                host_location=hardware,
                container_location="/input/hardware-passport.json",
                mode=MountMode.READ_ONLY,
            ),
        ]
        repository = None
        if isinstance(payload.model_ref, HuggingFacePlannerModelRef):
            repository = payload.model_ref.repository_id
        else:
            model_artifact = self._resolve_artifact(payload.model_ref.model_artifact)
            mounts.append(
                ContainerMount(
                    host_root=StorageRoot.OUTPUT,
                    host_location=model_artifact,
                    container_location="/input/model-artifact.json",
                    mode=MountMode.READ_ONLY,
                )
            )
        mounts.append(
            ContainerMount(
                host_root=StorageRoot.OUTPUT,
                host_location=output,
                container_location="/output",
                mode=MountMode.READ_WRITE,
            )
        )
        budget = payload.budget
        return ContainerSpec(
            kind=ContainerKind.PLANNER,
            name=self.planner_name(payload.job_id),
            image=image,
            entrypoint_profile=EntrypointProfile.PLANNER_PROVIDER_RUNTIME,
            network=self._policy.network,
            gpu_index=None,
            exclusive_gpu=False,
            pid_limit=budget.pid_limit,
            task_timeout_seconds=budget.max_duration_seconds,
            resource_limits=ContainerResourceLimits(
                cpu_millis=budget.cpu_millis,
                memory_bytes=budget.max_memory_bytes,
                max_disk_growth_bytes=budget.max_disk_growth_bytes,
            ),
            mounts=tuple(mounts),
            credentials=(),
            planner_arguments=PlannerProviderArguments(
                model_source=payload.model_ref.type,
                model_repository=repository,
                model_revision=str(payload.model_ref.revision),
            ),
            labels=(
                ("autopilot.managed", "true"),
                ("autopilot.kind", ContainerKind.PLANNER.value),
                ("autopilot.resource", payload.job_id),
            ),
        )

    @staticmethod
    def deployment_name(deployment_id: str) -> ContainerName:
        return f"vllm-{deployment_id.removeprefix('deployment_')}"

    @staticmethod
    def benchmark_name(benchmark_id: str) -> ContainerName:
        return f"evalscope-{benchmark_id.removeprefix('benchmark_')}"

    @staticmethod
    def planner_name(job_id: str) -> ContainerName:
        return f"planner-{job_id.removeprefix('job_')}"

    def _resolve_artifact(self, artifact: RunnerArtifactInput) -> Path:
        typed = artifact
        resolved = self._roots.resolve(
            typed.storage,
            must_exist=True,
            require_directory=False,
        )
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            with resolved.open("rb") as stream:
                while chunk := stream.read(1_048_576):
                    size_bytes += len(chunk)
                    if size_bytes > typed.size_bytes:
                        raise PathBoundaryError(
                            "Artifact size exceeds its immutable input metadata"
                        )
                    digest.update(chunk)
        except OSError as error:
            raise PathBoundaryError("Artifact input cannot be read") from error
        if size_bytes != typed.size_bytes or f"sha256:{digest.hexdigest()}" != str(typed.sha256):
            raise PathBoundaryError("Artifact input does not match its immutable metadata")
        return resolved


def stop_and_remove(
    adapter: DockerAdapter,
    handle: ContainerHandle,
    *,
    timeout_seconds: int,
) -> None:
    """Best-effort typed cleanup with a classified failure."""

    failures: list[str] = []
    try:
        if handle.state not in {ContainerState.EXITED, ContainerState.REMOVED}:
            adapter.stop(handle.container_id, timeout_seconds=timeout_seconds)
    except ResourceNotFoundError:
        return
    except DockerOperationError as error:
        failures.append(str(error))
    try:
        adapter.remove(handle.container_id)
    except ResourceNotFoundError:
        pass
    except DockerOperationError as error:
        failures.append(str(error))
    if failures:
        raise DockerOperationError("container cleanup did not complete")
