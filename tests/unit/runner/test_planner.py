import hashlib

from pydantic import TypeAdapter

from runner.docker import ContainerKind, EntrypointProfile
from runner.fakes import FakeDockerAdapter
from runner.models import RunnerRequest, StartCapacityPlannerRequest, StorageRoot
from runner.paths import RootRegistry
from runner.service import RunnerService
from tests.unit.runner.conftest import DIGEST, OTHER_DIGEST, PLAN_ID


def _artifact(roots: RootRegistry, name: str, content: bytes) -> dict[str, object]:
    location = roots.root(StorageRoot.OUTPUT) / "inputs" / name
    location.parent.mkdir(parents=True, exist_ok=True)
    location.write_bytes(content)
    return {
        "artifact_id": "artifact_" + hashlib.md5(name.encode(), usedforsecurity=False).hexdigest(),
        "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
        "content_type": "application/json",
        "size_bytes": len(content),
        "storage": {
            "root": "output",
            "relative_path": f"inputs/{name}",
        },
    }


def _start_data(roots: RootRegistry) -> dict[str, object]:
    return {
        "request_id": "request-planner-1",
        "idempotency_key": DIGEST,
        "actor": "autopilot-worker",
        "action": "start_capacity_planner",
        "plan_id": PLAN_ID,
        "plan_hash": DIGEST,
        "payload": {
            "job_id": "job_" + "7" * 32,
            "model_ref": {
                "type": "huggingface",
                "repository_id": "Qwen/Qwen3-8B",
                "revision": "8" * 40,
                "config_artifact": _artifact(roots, "model-config.json", b'{"layers": 32}'),
            },
            "hardware_passport_artifact": _artifact(
                roots,
                "hardware-passport.json",
                b'{"gpu": "RTX 5090", "memory_bytes": 34359738368}',
            ),
            "tensor_parallel_size": 1,
            "budget": {
                "max_duration_seconds": 120,
                "cpu_millis": 2000,
                "max_memory_bytes": 2_147_483_648,
                "pid_limit": 256,
                "max_disk_growth_bytes": 100_000_000,
            },
        },
    }


def test_capacity_planner_uses_fixed_digest_artifacts_and_budgets(
    runner_service: RunnerService,
    fake_docker: FakeDockerAdapter,
    roots: RootRegistry,
) -> None:
    parsed = TypeAdapter(RunnerRequest).validate_python(_start_data(roots))
    assert isinstance(parsed, StartCapacityPlannerRequest)
    response = runner_service.dispatch(parsed)
    assert response.accepted is True

    specification = fake_docker.specifications[0]
    assert specification.kind is ContainerKind.PLANNER
    assert specification.entrypoint_profile is EntrypointProfile.PLANNER_PROVIDER_RUNTIME
    assert specification.gpu_index is None
    assert specification.exclusive_gpu is False
    assert specification.resource_limits.cpu_millis == 2000
    assert specification.resource_limits.memory_bytes == 2_147_483_648
    assert specification.planner_arguments is not None
    assert specification.planner_arguments.tensor_parallel_size == 1
    assert [mount.container_location for mount in specification.mounts] == [
        "/input/model-config.json",
        "/input/hardware-passport.json",
        "/output",
    ]


def test_capacity_planner_status_cancel_and_artifact_surface(
    runner_service: RunnerService,
    fake_docker: FakeDockerAdapter,
    roots: RootRegistry,
) -> None:
    adapter = TypeAdapter(RunnerRequest)
    assert runner_service.dispatch(adapter.validate_python(_start_data(roots))).accepted is True
    common = {
        "actor": "autopilot-worker",
        "plan_id": PLAN_ID,
        "plan_hash": DIGEST,
        "payload": {"job_id": "job_" + "7" * 32},
    }
    status = adapter.validate_python(
        {
            **common,
            "request_id": "request-planner-status",
            "idempotency_key": OTHER_DIGEST,
            "action": "get_capacity_planner_status",
        }
    )
    status_response = runner_service.dispatch(status)
    assert status_response.result is not None
    assert status_response.result.state == "running"

    artifacts = adapter.validate_python(
        {
            **common,
            "request_id": "request-planner-artifacts",
            "idempotency_key": "sha256:" + "c" * 64,
            "action": "get_capacity_planner_artifacts",
        }
    )
    assert runner_service.dispatch(artifacts).accepted is True

    cancel = adapter.validate_python(
        {
            **common,
            "request_id": "request-planner-cancel",
            "idempotency_key": "sha256:" + "d" * 64,
            "action": "cancel_capacity_planner",
        }
    )
    assert runner_service.dispatch(cancel).accepted is True
    assert fake_docker.handles == {}


def test_capacity_planner_rejects_tampered_artifact_before_docker(
    runner_service: RunnerService,
    fake_docker: FakeDockerAdapter,
    roots: RootRegistry,
) -> None:
    data = _start_data(roots)
    parsed = TypeAdapter(RunnerRequest).validate_python(data)
    config_path = roots.root(StorageRoot.OUTPUT) / "inputs" / "model-config.json"
    config_path.write_bytes(b'{"tampered": true}')
    response = runner_service.dispatch(parsed)
    assert response.accepted is False
    assert response.error is not None
    assert response.error.code == "PATH_BOUNDARY_VIOLATION"
    assert fake_docker.specifications == []
