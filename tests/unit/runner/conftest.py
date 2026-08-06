from pathlib import Path

import pytest

from runner.docker import ContainerKind, ContainerSpecCompiler, RunnerDockerPolicy
from runner.fakes import FakeDockerAdapter, FakeNonDockerOperations, FakeVllmHealthProbe
from runner.health import VllmHealthVerifier
from runner.leases import GpuLeaseManager
from runner.models import ImageDigest, StorageRoot
from runner.paths import RootRegistry
from runner.service import RunnerService

HEX = "1" * 32
PLAN_ID = f"plan_{HEX}"
WORKER_ID = f"worker_{'2' * 32}"
DEPLOYMENT_ID = f"deployment_{'3' * 32}"
BENCHMARK_ID = f"benchmark_{'4' * 32}"
JOB_ID = f"job_{'4' * 32}"
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
VLLM_IMAGE_VALUE = "vllm/vllm-openai@sha256:" + "c" * 64
EVALSCOPE_IMAGE_VALUE = "modelscope/evalscope@sha256:" + "d" * 64
PLANNER_IMAGE_VALUE = "ghcr.io/llm-d-incubation/planner@sha256:" + "f" * 64
MODEL_REVISION = "e" * 40


@pytest.fixture
def roots(tmp_path: Path) -> RootRegistry:
    return RootRegistry(
        {
            StorageRoot.MODEL_CACHE: tmp_path / "model-cache",
            StorageRoot.OUTPUT: tmp_path / "output",
            StorageRoot.TEMPORARY: tmp_path / "temporary",
            StorageRoot.RUNTIME: tmp_path / "runtime",
        }
    )


@pytest.fixture
def docker_policy() -> RunnerDockerPolicy:
    vllm = ImageDigest(root=VLLM_IMAGE_VALUE)
    evalscope = ImageDigest(root=EVALSCOPE_IMAGE_VALUE)
    planner = ImageDigest(root=PLANNER_IMAGE_VALUE)
    return RunnerDockerPolicy(
        network="autopilot-runtime",
        allowed_images={
            ContainerKind.VLLM: frozenset({vllm}),
            ContainerKind.EVALSCOPE: frozenset({evalscope}),
            ContainerKind.PLANNER: frozenset({planner}),
            ContainerKind.CUDA_PROBE: frozenset(),
        },
        evalscope_image=evalscope,
        planner_image=planner,
    )


@pytest.fixture
def fake_docker() -> FakeDockerAdapter:
    return FakeDockerAdapter()


@pytest.fixture
def fake_health_probe() -> FakeVllmHealthProbe:
    return FakeVllmHealthProbe()


@pytest.fixture
def runner_service(
    roots: RootRegistry,
    docker_policy: RunnerDockerPolicy,
    fake_docker: FakeDockerAdapter,
    fake_health_probe: FakeVllmHealthProbe,
) -> RunnerService:
    compiler = ContainerSpecCompiler(roots=roots, policy=docker_policy)
    return RunnerService(
        docker=fake_docker,
        compiler=compiler,
        leases=GpuLeaseManager(),
        non_docker=FakeNonDockerOperations(),
        health=VllmHealthVerifier(fake_health_probe),
    )


def start_deployment_data(*, idempotency: str = DIGEST) -> dict[str, object]:
    return {
        "request_id": "request-deploy-1",
        "idempotency_key": idempotency,
        "actor": "autopilot-worker",
        "action": "start_deployment",
        "plan_id": PLAN_ID,
        "plan_hash": DIGEST,
        "payload": {
            "deployment_id": DEPLOYMENT_ID,
            "worker_id": WORKER_ID,
            "image": VLLM_IMAGE_VALUE,
            "model_repository": "Qwen/Qwen3-8B",
            "model_revision": MODEL_REVISION,
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
        },
    }
