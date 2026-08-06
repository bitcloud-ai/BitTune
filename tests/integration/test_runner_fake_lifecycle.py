from pathlib import Path

from pydantic import TypeAdapter

from runner.docker import ContainerKind, ContainerSpecCompiler, RunnerDockerPolicy
from runner.fakes import FakeDockerAdapter, FakeNonDockerOperations, FakeVllmHealthProbe
from runner.health import VllmHealthVerifier
from runner.leases import GpuLeaseManager
from runner.models import ImageDigest, RunnerRequest, StorageRoot
from runner.paths import RootRegistry
from runner.service import RunnerService
from tests.unit.runner.conftest import (
    DEPLOYMENT_ID,
    DIGEST,
    EVALSCOPE_IMAGE_VALUE,
    OTHER_DIGEST,
    PLAN_ID,
    VLLM_IMAGE_VALUE,
    start_deployment_data,
)


def test_worker_runner_fake_lifecycle_is_idempotent_and_cleans_resources(tmp_path: Path) -> None:
    roots = RootRegistry(
        {
            StorageRoot.MODEL_CACHE: tmp_path / "model-cache",
            StorageRoot.OUTPUT: tmp_path / "output",
            StorageRoot.TEMPORARY: tmp_path / "temporary",
            StorageRoot.RUNTIME: tmp_path / "runtime",
        }
    )
    vllm = ImageDigest(root=VLLM_IMAGE_VALUE)
    evalscope = ImageDigest(root=EVALSCOPE_IMAGE_VALUE)
    policy = RunnerDockerPolicy(
        network="autopilot-runtime",
        allowed_images={
            ContainerKind.VLLM: frozenset({vllm}),
            ContainerKind.EVALSCOPE: frozenset({evalscope}),
            ContainerKind.PLANNER: frozenset(),
            ContainerKind.CUDA_PROBE: frozenset(),
        },
        evalscope_image=evalscope,
    )
    docker = FakeDockerAdapter()
    service = RunnerService(
        docker=docker,
        compiler=ContainerSpecCompiler(roots=roots, policy=policy),
        leases=GpuLeaseManager(),
        non_docker=FakeNonDockerOperations(),
        health=VllmHealthVerifier(FakeVllmHealthProbe()),
    )
    adapter = TypeAdapter(RunnerRequest)

    start = adapter.validate_python(start_deployment_data())
    assert service.dispatch(start).accepted is True
    assert service.dispatch(start).idempotent_replay is True

    status = adapter.validate_python(
        {
            "request_id": "request-status-1",
            "idempotency_key": OTHER_DIGEST,
            "actor": "autopilot-worker",
            "action": "get_deployment_status",
            "plan_id": PLAN_ID,
            "plan_hash": DIGEST,
            "payload": {"deployment_id": DEPLOYMENT_ID},
        }
    )
    status_response = service.dispatch(status)
    assert status_response.result is not None
    assert status_response.result.state == "running"

    stop = adapter.validate_python(
        {
            "request_id": "request-stop-1",
            "idempotency_key": "sha256:" + "f" * 64,
            "actor": "autopilot-worker",
            "action": "stop_deployment",
            "plan_id": PLAN_ID,
            "plan_hash": DIGEST,
            "payload": {"deployment_id": DEPLOYMENT_ID},
        }
    )
    assert service.dispatch(stop).accepted is True
    assert docker.handles == {}
