"""vLLM adapter over the typed Unix-socket Host Runner contract."""

from __future__ import annotations

from typing import Protocol

from autopilot.capabilities.deployment.domain.enums import (
    DeploymentProviderState,
    DeploymentValidationCode,
)
from autopilot.capabilities.deployment.domain.errors import DeploymentProviderError
from autopilot.capabilities.deployment.domain.models import (
    CompiledVllmDeployment,
    VllmVersionProfile,
)
from autopilot.capabilities.deployment.ports.models import (
    DeploymentAdapterCapabilities,
    DeploymentOperation,
    DeploymentStartContext,
)
from autopilot.domain.identifiers import DeploymentId
from autopilot.domain.models import HuggingFaceModelRef
from runner.models import (
    DeploymentRefPayload,
    DeploymentStartPayload,
    DeploymentStatusRequest,
    ImageDigest,
    ModelRevision,
    RelativeStoragePath,
    RunnerRequest,
    RunnerResponse,
    Sha256Digest,
    StartDeploymentRequest,
    StopDeploymentRequest,
    StorageRef,
    StorageRoot,
    VllmParameters,
)


class RunnerDispatcher(Protocol):
    """Transport-neutral typed Runner request dispatcher."""

    def dispatch(self, request: RunnerRequest) -> RunnerResponse: ...


class VllmRunnerAdapter:
    """Forward one already compiled vLLM plan to the Host Runner."""

    def __init__(
        self,
        *,
        profile: VllmVersionProfile | None,
        runner: RunnerDispatcher | None,
    ) -> None:
        self._profile = profile
        self._runner = runner

    def capabilities(self) -> DeploymentAdapterCapabilities:
        return DeploymentAdapterCapabilities()

    def validate(self, compiled: CompiledVllmDeployment) -> None:
        profile = self._profile
        if profile is None or self._runner is None:
            raise DeploymentProviderError(
                DeploymentValidationCode.PROVIDER_UNAVAILABLE,
                "the G0-verified vLLM Provider profile and Runner are not configured",
                retryable=False,
            )
        if (
            compiled.provider_version != profile.provider_version
            or compiled.adapter_version != profile.adapter_version
            or compiled.provider_profile_version != profile.profile_version
            or compiled.engine_image != profile.engine_image
        ):
            raise DeploymentProviderError(
                DeploymentValidationCode.PROFILE_UNVERIFIED,
                "compiled deployment does not match the registered vLLM profile",
                retryable=False,
            )
        if not isinstance(compiled.model_ref, HuggingFaceModelRef):
            raise DeploymentProviderError(
                DeploymentValidationCode.MODEL_REF_UNSUPPORTED,
                "the MVP Runner only accepts immutable Hugging Face model references",
                retryable=False,
            )

    def start(
        self,
        compiled: CompiledVllmDeployment,
        context: DeploymentStartContext,
    ) -> DeploymentOperation:
        self.validate(compiled)
        model = compiled.model_ref
        if not isinstance(model, HuggingFaceModelRef):
            raise DeploymentProviderError(
                DeploymentValidationCode.MODEL_REF_UNSUPPORTED,
                "the MVP Runner only accepts immutable Hugging Face model references",
                retryable=False,
            )
        request = StartDeploymentRequest(
            request_id=context.request_id,
            idempotency_key=Sha256Digest(root=context.idempotency_key.root),
            actor="autopilot-worker",
            plan_id=str(context.plan_id),
            plan_hash=Sha256Digest(root=context.plan_hash.root),
            payload=DeploymentStartPayload(
                deployment_id=str(context.deployment_id),
                worker_id=str(context.worker_id),
                image=ImageDigest(root=str(compiled.engine_image)),
                model_repository=model.repository_id,
                model_revision=ModelRevision(root=model.revision.root),
                model_cache=StorageRef(
                    root=StorageRoot.MODEL_CACHE,
                    relative_path=RelativeStoragePath(
                        root=f"models/{model.repository_id}/{model.revision.root}"
                    ),
                ),
                parameters=VllmParameters.model_validate(compiled.arguments.model_dump()),
                pid_limit=compiled.runtime_limits.pid_limit,
                startup_timeout_seconds=compiled.runtime_limits.startup_timeout_seconds,
                task_timeout_seconds=compiled.runtime_limits.task_timeout_seconds,
                max_disk_growth_bytes=compiled.runtime_limits.max_disk_growth_bytes,
            ),
        )
        return self._dispatch(request, context.deployment_id)

    def status(self, context: DeploymentStartContext) -> DeploymentOperation:
        request = DeploymentStatusRequest(
            request_id=context.request_id,
            idempotency_key=Sha256Digest(root=context.idempotency_key.root),
            actor="autopilot-worker",
            plan_id=str(context.plan_id),
            plan_hash=Sha256Digest(root=context.plan_hash.root),
            payload=DeploymentRefPayload(deployment_id=str(context.deployment_id)),
        )
        return self._dispatch(request, context.deployment_id)

    def cancel(self, context: DeploymentStartContext) -> DeploymentOperation:
        request = StopDeploymentRequest(
            request_id=context.request_id,
            idempotency_key=Sha256Digest(root=context.idempotency_key.root),
            actor="autopilot-worker",
            plan_id=str(context.plan_id),
            plan_hash=Sha256Digest(root=context.plan_hash.root),
            payload=DeploymentRefPayload(deployment_id=str(context.deployment_id)),
        )
        return self._dispatch(request, context.deployment_id)

    def _dispatch(
        self,
        request: RunnerRequest,
        deployment_id: DeploymentId,
    ) -> DeploymentOperation:
        response = self._require_runner().dispatch(request)
        if not response.accepted or response.result is None:
            error = response.error
            raise DeploymentProviderError(
                DeploymentValidationCode.RUNNER_REJECTED,
                error.message if error is not None else "Host Runner rejected the operation",
                retryable=error.retryable if error is not None else False,
            )
        state_map = {
            "accepted": DeploymentProviderState.ACCEPTED,
            "running": DeploymentProviderState.RUNNING,
            "succeeded": DeploymentProviderState.HEALTHY,
            "stopped": DeploymentProviderState.STOPPED,
            "cancelled": DeploymentProviderState.STOPPED,
            "failed": DeploymentProviderState.FAILED,
        }
        return DeploymentOperation(
            deployment_id=deployment_id,
            state=state_map[response.result.state],
            provider_resource_id=response.result.resource_id,
            idempotent_replay=response.idempotent_replay,
            detail=response.result.detail,
        )

    def _require_runner(self) -> RunnerDispatcher:
        if self._runner is None:
            raise DeploymentProviderError(
                DeploymentValidationCode.PROVIDER_UNAVAILABLE,
                "the typed Host Runner client is not configured",
                retryable=False,
            )
        return self._runner
