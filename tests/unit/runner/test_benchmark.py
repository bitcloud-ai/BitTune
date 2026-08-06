import hashlib
from datetime import UTC, datetime, timedelta

from pydantic import TypeAdapter

from runner.docker import ContainerKind, ContainerSpecCompiler, RunnerDockerPolicy
from runner.fakes import FakeDockerAdapter, FakeNonDockerOperations, FakeVllmHealthProbe
from runner.health import VllmHealthVerifier
from runner.leases import GpuLeaseManager
from runner.models import RunnerRequest, StartBenchmarkRequest, StorageRoot
from runner.paths import RootRegistry
from runner.service import RunnerService, RunnerServiceConfig
from tests.unit.runner.conftest import (
    BENCHMARK_ID,
    DEPLOYMENT_ID,
    DIGEST,
    PLAN_ID,
)


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 6, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _artifact(roots: RootRegistry, content: bytes) -> dict[str, object]:
    location = roots.root(StorageRoot.OUTPUT) / "inputs" / "benchmark-spec.json"
    location.parent.mkdir(parents=True, exist_ok=True)
    location.write_bytes(content)
    return {
        "artifact_id": "artifact_" + "6" * 32,
        "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
        "content_type": "application/json",
        "size_bytes": len(content),
        "storage": {
            "root": "output",
            "relative_path": "inputs/benchmark-spec.json",
        },
    }


def _start_data(
    roots: RootRegistry,
    *,
    max_duration_seconds: int = 120,
    max_disk_growth_bytes: int = 100_000_000,
) -> dict[str, object]:
    return {
        "request_id": "request-benchmark-1",
        "idempotency_key": DIGEST,
        "actor": "autopilot-worker",
        "action": "start_benchmark",
        "plan_id": PLAN_ID,
        "plan_hash": DIGEST,
        "payload": {
            "benchmark_id": BENCHMARK_ID,
            "deployment_id": DEPLOYMENT_ID,
            "compiled_spec_artifact": _artifact(roots, b'{"mode": "baseline"}'),
            "max_duration_seconds": max_duration_seconds,
            "max_requests": 100,
            "max_input_tokens": 100_000,
            "max_output_tokens": 50_000,
            "cpu_millis": 2000,
            "max_memory_bytes": 2_147_483_648,
            "pid_limit": 256,
            "max_disk_growth_bytes": max_disk_growth_bytes,
        },
    }


def _service(
    *,
    roots: RootRegistry,
    policy: RunnerDockerPolicy,
    docker: FakeDockerAdapter,
    clock: MutableClock,
) -> RunnerService:
    return RunnerService(
        docker=docker,
        compiler=ContainerSpecCompiler(roots=roots, policy=policy),
        leases=GpuLeaseManager(clock=clock),
        non_docker=FakeNonDockerOperations(),
        health=VllmHealthVerifier(FakeVllmHealthProbe()),
        config=RunnerServiceConfig(clock=clock),
    )


def test_benchmark_verifies_immutable_compiled_spec_before_docker(
    runner_service: RunnerService,
    fake_docker: FakeDockerAdapter,
    roots: RootRegistry,
) -> None:
    parsed = TypeAdapter(RunnerRequest).validate_python(_start_data(roots))
    assert isinstance(parsed, StartBenchmarkRequest)
    response = runner_service.dispatch(parsed)
    assert response.accepted is True

    specification = fake_docker.specifications[0]
    assert specification.kind is ContainerKind.EVALSCOPE
    assert specification.mounts[0].host_location.name == "benchmark-spec.json"
    assert specification.resource_limits.max_disk_growth_bytes == 100_000_000


def test_benchmark_rejects_tampered_compiled_spec_before_docker(
    runner_service: RunnerService,
    fake_docker: FakeDockerAdapter,
    roots: RootRegistry,
) -> None:
    parsed = TypeAdapter(RunnerRequest).validate_python(_start_data(roots))
    location = roots.root(StorageRoot.OUTPUT) / "inputs" / "benchmark-spec.json"
    location.write_bytes(b'{"tampered": true}')

    response = runner_service.dispatch(parsed)

    assert response.accepted is False
    assert response.error is not None
    assert response.error.code == "PATH_BOUNDARY_VIOLATION"
    assert fake_docker.specifications == []


def test_reconcile_cleans_benchmark_at_deadline(
    roots: RootRegistry,
    docker_policy: RunnerDockerPolicy,
    fake_docker: FakeDockerAdapter,
) -> None:
    clock = MutableClock()
    service = _service(roots=roots, policy=docker_policy, docker=fake_docker, clock=clock)
    request = TypeAdapter(RunnerRequest).validate_python(_start_data(roots, max_duration_seconds=1))
    assert service.dispatch(request).accepted is True
    name = fake_docker.specifications[0].name

    clock.now += timedelta(seconds=1)

    assert service.reconcile(
        expected_container_names=frozenset({name}),
        reconcilable_container_names=frozenset({name}),
    ) == (name,)
    assert fake_docker.handles == {}


def test_reconcile_cleans_benchmark_on_disk_growth(
    roots: RootRegistry,
    docker_policy: RunnerDockerPolicy,
    fake_docker: FakeDockerAdapter,
) -> None:
    clock = MutableClock()
    service = _service(roots=roots, policy=docker_policy, docker=fake_docker, clock=clock)
    request = TypeAdapter(RunnerRequest).validate_python(
        _start_data(roots, max_disk_growth_bytes=3)
    )
    assert service.dispatch(request).accepted is True
    name = fake_docker.specifications[0].name
    output = roots.root(StorageRoot.OUTPUT) / "benchmarks" / BENCHMARK_ID
    (output / "result.json").write_bytes(b"1234")

    assert service.reconcile(
        expected_container_names=frozenset({name}),
        reconcilable_container_names=frozenset({name}),
    ) == (name,)
    assert fake_docker.handles == {}
