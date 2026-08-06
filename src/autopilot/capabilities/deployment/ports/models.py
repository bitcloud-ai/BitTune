"""Typed vLLM Provider lifecycle contracts."""

from typing import Literal

from autopilot.capabilities.deployment.domain.enums import DeploymentProviderState
from autopilot.domain.base import NonEmptyStr, StrictModel
from autopilot.domain.identifiers import (
    DeploymentId,
    PlanHash,
    PlanId,
    Sha256Digest,
    WorkerId,
)


class DeploymentAdapterCapabilities(StrictModel):
    schema_version: Literal["deployment-adapter-capabilities/v1"] = (
        "deployment-adapter-capabilities/v1"
    )
    provider: Literal["vllm"] = "vllm"
    topology: Literal["single_gpu"] = "single_gpu"
    tensor_parallel_size: Literal[1] = 1
    accelerator_index: Literal[0] = 0
    supports_cancel: Literal[True] = True


class DeploymentStartContext(StrictModel):
    """Trusted execution binding supplied after Gateway authorization."""

    schema_version: Literal["deployment-start-context/v1"] = "deployment-start-context/v1"
    deployment_id: DeploymentId
    plan_id: PlanId
    plan_hash: PlanHash
    idempotency_key: Sha256Digest
    worker_id: WorkerId
    request_id: NonEmptyStr


class DeploymentOperation(StrictModel):
    schema_version: Literal["deployment-operation/v1"] = "deployment-operation/v1"
    deployment_id: DeploymentId
    state: DeploymentProviderState
    provider_resource_id: NonEmptyStr
    idempotent_replay: bool = False
    detail: NonEmptyStr | None = None
