# ruff: noqa: TRY003
"""Docker SDK implementation of the narrow :mod:`runner.docker` port.

The optional Docker dependency is imported only by the production runner
process.  The adapter receives a fixed ``ContainerSpec`` and never accepts a
generic Docker options object from the caller.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import import_module
from typing import Protocol, cast

from docker.errors import DockerException as _DockerError  # type: ignore[import-untyped]
from docker.errors import NotFound as _DockerNotFound
from docker.types import DeviceRequest as _DeviceRequest  # type: ignore[import-untyped]

from runner.docker import (
    ContainerHandle,
    ContainerKind,
    ContainerSpec,
    ContainerState,
    DockerAdapter,
    EntrypointProfile,
)
from runner.errors import DockerOperationError, ResourceNotFoundError, RunnerConfigurationError
from runner.logs import SecretRedactor
from runner.models import SecretRef
from runner.secrets import SystemdCredentialResolver


class _SdkContainer(Protocol):
    id: str
    name: str
    status: str
    labels: Mapping[str, str]

    def start(self) -> None: ...

    def stop(self, *, timeout: int) -> None: ...

    def remove(self, *, force: bool = False) -> None: ...

    def reload(self) -> None: ...


class _SdkContainerCollection(Protocol):
    def create(
        self,
        image: str,
        provider_arguments: tuple[str, ...],
        **options: object,
    ) -> _SdkContainer: ...

    def get(self, name: str) -> _SdkContainer: ...

    def list(self, **options: object) -> list[_SdkContainer]: ...


@dataclass(frozen=True, slots=True)
class _CreateOptions:
    name: str
    entrypoint: tuple[str, ...]
    environment: Mapping[str, str]
    volumes: Mapping[str, Mapping[str, str]]
    network: str
    pid_limit: int
    nano_cpus: int | None
    mem_limit: int | None
    read_only: bool
    cap_drop: tuple[str, ...]
    security_opt: tuple[str, ...]
    device_requests: tuple[object, ...]
    labels: Mapping[str, str]
    detach: bool
    auto_remove: bool


class _SdkClient(Protocol):
    containers: _SdkContainerCollection


class CredentialResolver(Protocol):
    def resolve(self, reference: SecretRef) -> bytes: ...


class DockerSdkAdapter(DockerAdapter):
    """Execute only fixed container lifecycle operations through Docker SDK."""

    def __init__(
        self,
        client: _SdkClient,
        *,
        credentials: CredentialResolver | None = None,
    ) -> None:
        self._client = client
        self._credentials = credentials

    @classmethod
    def from_environment(
        cls,
        *,
        redactor: SecretRedactor | None = None,
    ) -> DockerSdkAdapter:
        try:
            module = import_module("docker")
        except ImportError as error:
            raise RunnerConfigurationError("runner Docker SDK extra is not installed") from error
        try:
            factory = cast(Callable[[], object], module.__dict__["from_env"])
            credentials = None
            if "CREDENTIALS_DIRECTORY" in os.environ:
                credentials = SystemdCredentialResolver.from_environment(redactor=redactor)
            return cls(cast(_SdkClient, factory()), credentials=credentials)
        except _DockerError as error:
            raise RunnerConfigurationError("runner cannot connect to the Docker daemon") from error

    def create(self, specification: ContainerSpec) -> ContainerHandle:
        entrypoint = _entrypoint(specification.entrypoint_profile)
        provider_arguments = _provider_arguments(specification)
        environment = _fixed_environment(specification, self._credentials)
        volumes = _fixed_volumes(specification)
        device_requests: tuple[object, ...] = ()
        if specification.gpu_index == 0:
            device_requests = (_DeviceRequest(device_ids=["0"], capabilities=[["gpu"]]),)
        try:
            container = self._client.containers.create(
                str(specification.image),
                provider_arguments,
                **_create_options(
                    _CreateOptions(
                        name=specification.name,
                        entrypoint=entrypoint,
                        environment=environment,
                        volumes=volumes,
                        network=specification.network,
                        pid_limit=specification.pid_limit,
                        nano_cpus=(
                            None
                            if specification.resource_limits.cpu_millis is None
                            else specification.resource_limits.cpu_millis * 1_000_000
                        ),
                        mem_limit=specification.resource_limits.memory_bytes,
                        read_only=True,
                        cap_drop=("ALL",),
                        security_opt=("no-new-privileges:true",),
                        device_requests=device_requests,
                        labels=dict(specification.labels),
                        detach=True,
                        auto_remove=False,
                    )
                ),
            )
        except _DockerError as error:
            raise DockerOperationError("Docker container creation failed") from error
        return _to_handle(container, expected_kind=specification.kind)

    def start(self, container_id: str) -> ContainerHandle:
        try:
            container = self._client.containers.get(container_id)
            container.start()
            container.reload()
        except _DockerError as error:
            raise DockerOperationError("Docker container start failed") from error
        return _to_handle(container)

    def stop(self, container_id: str, *, timeout_seconds: int) -> ContainerHandle:
        try:
            container = self._client.containers.get(container_id)
            container.stop(timeout=timeout_seconds)
            container.reload()
        except _DockerError as error:
            raise DockerOperationError("Docker container stop failed") from error
        return _to_handle(container)

    def remove(self, container_id: str) -> None:
        try:
            container = self._client.containers.get(container_id)
            container.remove(force=False)
        except _DockerNotFound:
            raise ResourceNotFoundError("Docker container does not exist") from None
        except _DockerError as error:
            raise DockerOperationError("Docker container removal failed") from error

    def inspect(self, name: str) -> ContainerHandle | None:
        try:
            container = self._client.containers.get(name)
        except _DockerNotFound:
            return None
        except _DockerError as error:
            raise DockerOperationError("Docker container inspection failed") from error
        return _to_handle(container)

    def list_managed(self) -> tuple[ContainerHandle, ...]:
        try:
            containers = self._client.containers.list(
                all=True,
                filters={"label": "autopilot.managed=true"},
            )
        except _DockerError as error:
            raise DockerOperationError("Docker managed-container listing failed") from error
        handles = [_to_handle(container) for container in containers]
        return tuple(sorted(handles, key=lambda item: item.name))


def _entrypoint(profile: EntrypointProfile) -> tuple[str, ...]:
    if profile is EntrypointProfile.VLLM_OPENAI_SERVER:
        return ("vllm",)
    raise RunnerConfigurationError(
        "the requested provider runtime has no G0-verified entrypoint profile"
    )


def _provider_arguments(specification: ContainerSpec) -> tuple[str, ...]:
    if specification.entrypoint_profile is EntrypointProfile.VLLM_OPENAI_SERVER:
        vllm_arguments = specification.vllm_arguments
        if vllm_arguments is None:
            raise DockerOperationError("vLLM container arguments are missing")
        model_path = f"/models/{vllm_arguments.model_repository}"
        chunked_prefill_argument = (
            "--enable-chunked-prefill"
            if vllm_arguments.enable_chunked_prefill
            else "--no-enable-chunked-prefill"
        )
        return (
            "serve",
            model_path,
            "--tensor-parallel-size",
            str(vllm_arguments.tensor_parallel_size),
            "--max-model-len",
            str(vllm_arguments.max_model_len),
            "--gpu-memory-utilization",
            str(vllm_arguments.gpu_memory_utilization),
            "--max-num-seqs",
            str(vllm_arguments.max_num_seqs),
            "--max-num-batched-tokens",
            str(vllm_arguments.max_num_batched_tokens),
            chunked_prefill_argument,
            "--revision",
            vllm_arguments.model_revision,
        )
    return ()


def _fixed_environment(
    specification: ContainerSpec,
    resolver: CredentialResolver | None,
) -> dict[str, str]:
    environment = {"AUTOPILOT_CONTAINER_KIND": specification.kind.value}
    for credential in specification.credentials:
        if resolver is None:
            raise RunnerConfigurationError("systemd credential resolver is not configured")
        try:
            value = resolver.resolve(credential.secret_ref).decode("utf-8")
        except UnicodeDecodeError as error:
            raise RunnerConfigurationError("systemd credential is not valid UTF-8") from error
        if not value or any(character in value for character in ("\x00", "\r", "\n")):
            raise RunnerConfigurationError("systemd credential is not a valid environment value")
        environment[credential.variable] = value
    return environment


def _fixed_volumes(specification: ContainerSpec) -> dict[str, dict[str, str]]:
    return {
        str(mount.host_location): {
            "bind": mount.container_location,
            "mode": mount.mode.value,
        }
        for mount in specification.mounts
    }


def _create_options(options: _CreateOptions) -> dict[str, object]:
    values: dict[str, object | None] = {
        "name": options.name,
        "entrypoint": options.entrypoint,
        "environment": options.environment,
        "volumes": options.volumes,
        "network": options.network,
        "pids_limit": options.pid_limit,
        "nano_cpus": options.nano_cpus,
        "mem_limit": options.mem_limit,
        "read_only": options.read_only,
        "cap_drop": options.cap_drop,
        "security_opt": options.security_opt,
        "device_requests": options.device_requests,
        "labels": options.labels,
        "detach": options.detach,
        "auto_remove": options.auto_remove,
    }
    return {name: value for name, value in values.items() if value is not None}


def _to_handle(
    container: _SdkContainer,
    *,
    expected_kind: ContainerKind | None = None,
) -> ContainerHandle:
    kind_value = container.labels.get("autopilot.kind")
    if kind_value is None:
        raise DockerOperationError("Docker managed container has no kind label")
    try:
        resource_kind = ContainerKind(kind_value)
    except ValueError as error:
        raise DockerOperationError("Docker managed container kind label is invalid") from error
    if expected_kind is not None and resource_kind is not expected_kind:
        raise DockerOperationError("Docker container kind does not match the requested action")
    name = container.name.removeprefix("/")
    state = {
        "created": ContainerState.CREATED,
        "running": ContainerState.RUNNING,
        "exited": ContainerState.EXITED,
        "dead": ContainerState.FAILED,
        "paused": ContainerState.FAILED,
    }.get(container.status, ContainerState.FAILED)
    try:
        return ContainerHandle(
            container_id=container.id,
            name=name,
            kind=resource_kind,
            state=state,
        )
    except (KeyError, ValueError) as error:
        raise DockerOperationError("Docker managed container metadata is invalid") from error
