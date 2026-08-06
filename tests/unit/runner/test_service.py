from pydantic import TypeAdapter

from runner.docker import EntrypointProfile, MountMode
from runner.fakes import FakeDockerAdapter, FakeVllmHealthProbe
from runner.health import MinimalCompletionObservation, VllmHealthLayer
from runner.models import RunnerRequest, StartDeploymentRequest, StorageRoot
from runner.paths import RootRegistry
from runner.service import RunnerService
from tests.unit.runner.conftest import (
    DEPLOYMENT_ID,
    DIGEST,
    OTHER_DIGEST,
    PLAN_ID,
    start_deployment_data,
)


def _request(data: dict[str, object]) -> RunnerRequest:
    return TypeAdapter(RunnerRequest).validate_python(data)


def _start_request(*, idempotency: str = DIGEST) -> StartDeploymentRequest:
    parsed = _request(start_deployment_data(idempotency=idempotency))
    assert isinstance(parsed, StartDeploymentRequest)
    return parsed


def test_start_deployment_compiles_fixed_docker_boundary(
    runner_service: RunnerService,
    fake_docker: FakeDockerAdapter,
    fake_health_probe: FakeVllmHealthProbe,
) -> None:
    response = runner_service.dispatch(_start_request())
    assert response.accepted is True
    assert response.result is not None
    assert response.result.state == "running"
    assert response.result.lease is not None

    specification = fake_docker.specifications[0]
    assert specification.entrypoint_profile is EntrypointProfile.VLLM_OPENAI_SERVER
    assert specification.gpu_index == 0
    assert specification.exclusive_gpu is True
    assert specification.network == "autopilot-runtime"
    assert specification.vllm_arguments is not None
    assert specification.vllm_arguments.tensor_parallel_size == 1
    assert specification.vllm_arguments.trust_remote_code is False
    assert [(mount.host_root, mount.mode) for mount in specification.mounts] == [
        (StorageRoot.MODEL_CACHE, MountMode.READ_ONLY),
        (StorageRoot.OUTPUT, MountMode.READ_WRITE),
    ]
    assert fake_health_probe.calls == list(VllmHealthLayer)


def test_failed_health_check_cleans_container_and_gpu_lease(
    runner_service: RunnerService,
    fake_docker: FakeDockerAdapter,
    fake_health_probe: FakeVllmHealthProbe,
) -> None:
    fake_health_probe.completion_observation = MinimalCompletionObservation(
        succeeded=True,
        served_model_id="Qwen/Qwen3-8B",
        output_text="",
        output_tokens=0,
    )
    failed = runner_service.dispatch(_start_request())
    assert failed.accepted is False
    assert failed.error is not None
    assert failed.error.code == "VLLM_HEALTH_CHECK_FAILED"
    assert fake_docker.handles == {}

    fake_health_probe.completion_observation = MinimalCompletionObservation(
        succeeded=True,
        served_model_id="Qwen/Qwen3-8B",
        output_text="ok",
        output_tokens=1,
    )
    retry = runner_service.dispatch(_start_request(idempotency=OTHER_DIGEST))
    assert retry.accepted is True


def test_runner_replays_identical_request_and_rejects_key_reuse(
    runner_service: RunnerService,
    fake_docker: FakeDockerAdapter,
) -> None:
    first = runner_service.dispatch(_start_request())
    replay = runner_service.dispatch(_start_request())
    assert first.accepted is True
    assert replay.accepted is True
    assert replay.idempotent_replay is True
    assert len(fake_docker.specifications) == 1

    changed = start_deployment_data()
    changed["plan_hash"] = OTHER_DIGEST
    conflict = runner_service.dispatch(_request(changed))
    assert conflict.accepted is False
    assert conflict.error is not None
    assert conflict.error.code == "IDEMPOTENCY_CONFLICT"


def test_failed_container_start_cleans_container_and_gpu_lease(
    runner_service: RunnerService,
    fake_docker: FakeDockerAdapter,
) -> None:
    fake_docker.fail_start = True
    failed = runner_service.dispatch(_start_request())
    assert failed.accepted is False
    assert failed.error is not None
    assert failed.error.code == "DOCKER_OPERATION_FAILED"
    assert fake_docker.handles == {}

    fake_docker.fail_start = False
    retry = runner_service.dispatch(_start_request(idempotency=OTHER_DIGEST))
    assert retry.accepted is True


def test_stop_deployment_removes_container_and_releases_lease(
    runner_service: RunnerService,
    fake_docker: FakeDockerAdapter,
) -> None:
    assert runner_service.dispatch(_start_request()).accepted is True
    stop = _request(
        {
            "request_id": "request-stop-1",
            "idempotency_key": OTHER_DIGEST,
            "actor": "autopilot-worker",
            "action": "stop_deployment",
            "plan_id": PLAN_ID,
            "plan_hash": DIGEST,
            "payload": {"deployment_id": DEPLOYMENT_ID},
        }
    )
    response = runner_service.dispatch(stop)
    assert response.accepted is True
    assert response.result is not None
    assert response.result.state == "stopped"
    assert fake_docker.handles == {}


def test_temporary_cleanup_cannot_escape_registered_root(
    runner_service: RunnerService,
    roots: RootRegistry,
) -> None:
    target = roots.root(StorageRoot.TEMPORARY) / "experiments" / "one"
    target.mkdir(parents=True)
    (target / "result.json").write_text("{}", encoding="utf-8")
    cleanup = _request(
        {
            "request_id": "request-cleanup-1",
            "idempotency_key": OTHER_DIGEST,
            "actor": "autopilot-worker",
            "action": "cleanup_temporary",
            "plan_id": PLAN_ID,
            "plan_hash": DIGEST,
            "payload": {
                "temporary_ref": {
                    "root": "temporary",
                    "relative_path": "experiments/one",
                }
            },
        }
    )
    response = runner_service.dispatch(cleanup)
    assert response.accepted is True
    assert target.exists() is False


def test_reconcile_removes_only_unexpected_managed_containers(
    runner_service: RunnerService,
    fake_docker: FakeDockerAdapter,
) -> None:
    runner_service.dispatch(_start_request())
    name = fake_docker.specifications[0].name
    owned = frozenset({name})
    assert (
        runner_service.reconcile(
            expected_container_names=owned,
            reconcilable_container_names=owned,
        )
        == ()
    )
    cleaned = runner_service.reconcile(
        expected_container_names=frozenset(),
        reconcilable_container_names=owned,
    )
    assert cleaned == (name,)
    assert fake_docker.handles == {}


def test_reconcile_preserves_containers_without_authoritative_ownership(
    runner_service: RunnerService,
    fake_docker: FakeDockerAdapter,
) -> None:
    assert runner_service.dispatch(_start_request()).accepted is True
    assert (
        runner_service.reconcile(
            expected_container_names=frozenset(),
            reconcilable_container_names=frozenset(),
        )
        == ()
    )
    assert fake_docker.handles


def test_reconcile_cleans_deployment_on_disk_growth_and_releases_gpu(
    runner_service: RunnerService,
    fake_docker: FakeDockerAdapter,
    roots: RootRegistry,
) -> None:
    data = start_deployment_data()
    payload = data["payload"]
    assert isinstance(payload, dict)
    payload["max_disk_growth_bytes"] = 3
    assert runner_service.dispatch(_request(data)).accepted is True
    name = fake_docker.specifications[0].name
    output = roots.root(StorageRoot.OUTPUT) / "deployments" / DEPLOYMENT_ID
    (output / "server.log").write_bytes(b"1234")

    assert runner_service.reconcile(
        expected_container_names=frozenset({name}),
        reconcilable_container_names=frozenset({name}),
    ) == (name,)
    assert fake_docker.handles == {}
    assert runner_service.dispatch(_start_request(idempotency=OTHER_DIGEST)).accepted is True
