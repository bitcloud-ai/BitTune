import json

import pytest

from autopilot.capabilities.deployment.adapters.vllm import VllmRunnerAdapter
from autopilot.capabilities.deployment.application.compiler import compile_deployment
from autopilot.capabilities.deployment.domain.errors import DeploymentProviderError
from autopilot.capabilities.deployment.domain.models import (
    DeploymentExecutionSpecification,
    VllmVersionProfile,
)
from autopilot.capabilities.deployment.ports.models import DeploymentStartContext
from autopilot.domain.identifiers import (
    DeploymentId,
    PlanHash,
    PlanId,
    Sha256Digest,
    WorkerId,
)
from runner.models import OperationResponse, RunnerRequest, RunnerResponse


class FakeRunnerDispatcher:
    def __init__(self) -> None:
        self.requests: list[RunnerRequest] = []

    def dispatch(self, request: RunnerRequest) -> RunnerResponse:
        self.requests.append(request)
        return RunnerResponse(
            request_id=request.request_id,
            accepted=True,
            result=OperationResponse(
                resource_id=request.payload.deployment_id,
                state="running",
            ),
        )


def _context() -> DeploymentStartContext:
    return DeploymentStartContext(
        deployment_id=DeploymentId(root=f"deployment_{'1' * 32}"),
        plan_id=PlanId(root=f"plan_{'2' * 32}"),
        plan_hash=PlanHash(root=f"sha256:{'3' * 64}"),
        idempotency_key=Sha256Digest(root=f"sha256:{'4' * 64}"),
        worker_id=WorkerId(root=f"worker_{'5' * 32}"),
        request_id="request-deploy-1",
    )


def test_vllm_adapter_forwards_only_compiled_runner_fields(
    deployment_specification: DeploymentExecutionSpecification,
    vllm_profile: VllmVersionProfile,
) -> None:
    runner = FakeRunnerDispatcher()
    adapter = VllmRunnerAdapter(profile=vllm_profile, runner=runner)

    operation = adapter.start(
        compile_deployment(deployment_specification, vllm_profile),
        _context(),
    )

    assert operation.state == "running"
    assert len(runner.requests) == 1
    serialized = json.dumps(runner.requests[0].model_dump(mode="json"))
    assert all(
        forbidden not in serialized
        for forbidden in ("command", "entrypoint", "environment", "volume", "host_path")
    )


def test_vllm_adapter_fails_closed_without_verified_profile(
    deployment_specification: DeploymentExecutionSpecification,
    vllm_profile: VllmVersionProfile,
) -> None:
    compiled = compile_deployment(deployment_specification, vllm_profile)

    with pytest.raises(DeploymentProviderError) as caught:
        VllmRunnerAdapter(profile=None, runner=None).validate(compiled)

    assert caught.value.retryable is False
