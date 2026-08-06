from pathlib import Path

import pytest

from runner.docker import (
    ContainerKind,
    ContainerResourceLimits,
    ContainerSpec,
    ContainerSpecCompiler,
    EntrypointProfile,
    RunnerDockerPolicy,
)
from runner.docker_sdk import DockerSdkAdapter
from runner.errors import RunnerConfigurationError
from runner.models import DeploymentStartPayload, ImageDigest, StorageRoot
from runner.paths import RootRegistry

VLLM_IMAGE = "vllm/vllm-openai@sha256:" + "a" * 64
EVALSCOPE_IMAGE = "modelscope/evalscope@sha256:" + "b" * 64


class FakeSdkContainer:
    def __init__(self, name: str, labels: dict[str, str]) -> None:
        self.id = "c" * 64
        self.name = name
        self.status = "created"
        self.labels = labels

    def start(self) -> None:
        self.status = "running"

    def stop(self, *, timeout: int) -> None:
        del timeout
        self.status = "exited"

    def remove(self, *, force: bool = False) -> None:
        del force

    def reload(self) -> None:
        pass


class FakeSdkContainers:
    def __init__(self) -> None:
        self.created: tuple[str, tuple[str, ...], dict[str, object]] | None = None
        self.container: FakeSdkContainer | None = None

    def create(self, image: str, provider_arguments: tuple[str, ...], **options: object):
        labels = dict(options["labels"])
        self.created = (image, provider_arguments, options)
        self.container = FakeSdkContainer(str(options["name"]), labels)
        return self.container

    def get(self, name: str):
        del name
        assert self.container is not None
        return self.container

    def list(self, **options: object):
        del options
        return [] if self.container is None else [self.container]


class FakeSdkClient:
    def __init__(self) -> None:
        self.containers = FakeSdkContainers()


def test_docker_sdk_receives_only_fixed_compiled_options(tmp_path: Path) -> None:
    roots = RootRegistry(
        {
            StorageRoot.MODEL_CACHE: tmp_path / "model-cache",
            StorageRoot.OUTPUT: tmp_path / "output",
            StorageRoot.TEMPORARY: tmp_path / "temporary",
            StorageRoot.RUNTIME: tmp_path / "runtime",
        }
    )
    vllm = ImageDigest(root=VLLM_IMAGE)
    evalscope = ImageDigest(root=EVALSCOPE_IMAGE)
    compiler = ContainerSpecCompiler(
        roots=roots,
        policy=RunnerDockerPolicy(
            network="autopilot-runtime",
            allowed_images={
                ContainerKind.VLLM: frozenset({vllm}),
                ContainerKind.EVALSCOPE: frozenset({evalscope}),
                ContainerKind.PLANNER: frozenset(),
                ContainerKind.CUDA_PROBE: frozenset(),
            },
            evalscope_image=evalscope,
        ),
    )
    payload = DeploymentStartPayload.model_validate(
        {
            "deployment_id": "deployment_" + "1" * 32,
            "worker_id": "worker_" + "2" * 32,
            "image": VLLM_IMAGE,
            "model_repository": "Qwen/Qwen3-8B",
            "model_revision": "3" * 40,
            "model_cache": {
                "root": "model-cache",
                "relative_path": "qwen/qwen3-8b",
            },
            "parameters": {
                "tensor_parallel_size": 1,
                "max_model_len": 8192,
                "gpu_memory_utilization": 0.9,
                "max_num_seqs": 8,
                "max_num_batched_tokens": 4096,
                "enable_chunked_prefill": True,
                "trust_remote_code": False,
            },
            "pid_limit": 1024,
            "startup_timeout_seconds": 60,
            "task_timeout_seconds": 120,
            "max_disk_growth_bytes": 1_000_000_000,
        }
    )
    client = FakeSdkClient()
    handle = DockerSdkAdapter(client).create(compiler.deployment(payload))
    assert handle.kind is ContainerKind.VLLM
    assert client.containers.created is not None
    image, provider_arguments, options = client.containers.created
    assert image == VLLM_IMAGE
    assert provider_arguments[:2] == ("serve", "/models/Qwen/Qwen3-8B")
    assert options["entrypoint"] == ("vllm",)
    assert options["network"] == "autopilot-runtime"
    assert options["pids_limit"] == 1024
    assert options["read_only"] is True
    assert options["cap_drop"] == ("ALL",)
    assert options["security_opt"] == ("no-new-privileges:true",)
    assert len(options["device_requests"]) == 1
    assert "privileged" not in options


@pytest.mark.parametrize(
    ("kind", "profile", "image"),
    (
        (
            ContainerKind.EVALSCOPE,
            EntrypointProfile.EVALSCOPE_PROVIDER_RUNTIME,
            EVALSCOPE_IMAGE,
        ),
        (
            ContainerKind.PLANNER,
            EntrypointProfile.PLANNER_PROVIDER_RUNTIME,
            "ghcr.io/llm-d-incubation/planner@sha256:" + "c" * 64,
        ),
    ),
)
def test_unverified_provider_entrypoints_fail_closed(
    kind: ContainerKind,
    profile: EntrypointProfile,
    image: str,
) -> None:
    specification_data: dict[str, object] = {
        "kind": kind,
        "name": f"{kind.value}-fixture",
        "image": image,
        "entrypoint_profile": profile,
        "network": "autopilot-runtime",
        "gpu_index": None,
        "exclusive_gpu": False,
        "pid_limit": 128,
        "task_timeout_seconds": 60,
        "resource_limits": ContainerResourceLimits(
            cpu_millis=1000,
            memory_bytes=1_073_741_824,
            max_disk_growth_bytes=100_000_000,
        ),
        "mounts": (),
        "credentials": (),
        "labels": (
            ("autopilot.managed", "true"),
            ("autopilot.kind", kind.value),
        ),
    }
    if kind is ContainerKind.PLANNER:
        specification_data["planner_arguments"] = {
            "model_source": "huggingface",
            "model_repository": "Qwen/Qwen3-8B",
            "model_revision": "d" * 40,
        }
    specification = ContainerSpec.model_validate(specification_data)
    client = FakeSdkClient()
    with pytest.raises(RunnerConfigurationError):
        DockerSdkAdapter(client).create(specification)
    assert client.containers.created is None
