"""Pinned llm-d Capacity Planner adapter."""

from __future__ import annotations

from typing import Protocol

from autopilot.capabilities.capacity.application.validator import validate_capacity_specification
from autopilot.capabilities.capacity.domain.errors import CapacityProviderUnavailableError
from autopilot.capabilities.capacity.domain.models import (
    CapacityPlannerVersionProfile,
    CapacityPlanningSpecification,
    PlannerRawEstimate,
)
from autopilot.capabilities.capacity.ports import PlannerExecutionClient
from autopilot.capabilities.capacity.ports.lifecycle import (
    CapacityPlannerExecutionContext,
    CapacityPlannerOperation,
    PlannerArtifactLocator,
    PlannerResultReader,
)
from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.identifiers import JobId
from runner.models import (
    ArtifactPlannerModelRef,
    CancelCapacityPlannerRequest,
    CapacityPlannerArtifactsRequest,
    CapacityPlannerJobRefPayload,
    CapacityPlannerStartPayload,
    CapacityPlannerStatusRequest,
    HuggingFacePlannerModelRef,
    PlannerModelRef,
    RunnerArtifactInput,
    RunnerRequest,
    RunnerResponse,
    StartCapacityPlannerRequest,
)
from runner.models import (
    Sha256Digest as RunnerDigest,
)


class RunnerDispatcher(Protocol):
    def dispatch(self, request: RunnerRequest) -> RunnerResponse: ...


class LlmdPlannerAdapter:
    """Anti-corruption adapter over a typed Host Runner Planner client."""

    def __init__(
        self,
        *,
        profile: CapacityPlannerVersionProfile,
        client: PlannerExecutionClient | None,
    ) -> None:
        self._profile = profile
        self._client = client

    @property
    def profile(self) -> CapacityPlannerVersionProfile:
        return self._profile

    def validate(self, specification: CapacityPlanningSpecification) -> None:
        validate_capacity_specification(specification, self._profile)
        if self._client is None:
            raise CapacityProviderUnavailableError

    def estimate(self, specification: CapacityPlanningSpecification) -> PlannerRawEstimate:
        self.validate(specification)
        client = self._client
        if client is None:
            raise CapacityProviderUnavailableError
        return client.estimate(specification, self._profile)


class LlmdPlannerRunnerAdapter:
    """Asynchronous Planner lifecycle over typed Runner actions."""

    def __init__(
        self,
        *,
        profile: CapacityPlannerVersionProfile | None,
        runner: RunnerDispatcher | None,
        locator: PlannerArtifactLocator | None,
        results: PlannerResultReader | None,
    ) -> None:
        self._profile = profile
        self._runner = runner
        self._locator = locator
        self._results = results

    @property
    def profile(self) -> CapacityPlannerVersionProfile:
        profile = self._profile
        if profile is None:
            raise CapacityProviderUnavailableError
        return profile

    def start(
        self,
        specification: CapacityPlanningSpecification,
        context: CapacityPlannerExecutionContext,
    ) -> CapacityPlannerOperation:
        self._validate_bindings(specification, context)
        locator = self._locator
        runner = self._runner
        if locator is None or runner is None:
            raise CapacityProviderUnavailableError
        model_ref: PlannerModelRef
        config = self._runner_artifact(context.model_config_artifact, locator)
        if specification.model_ref.type == "huggingface":
            model_ref = HuggingFacePlannerModelRef(
                repository_id=specification.model_ref.repository_id,
                revision=specification.model_ref.revision.root,
                config_artifact=config,
            )
        else:
            if context.model_artifact is None:
                raise CapacityProviderUnavailableError
            model_ref = ArtifactPlannerModelRef(
                model_artifact=self._runner_artifact(context.model_artifact, locator),
                revision=specification.model_ref.revision.root,
                config_artifact=config,
            )
        request = StartCapacityPlannerRequest(
            request_id=context.request_id,
            idempotency_key=RunnerDigest(root=context.idempotency_key.root),
            actor="autopilot-worker",
            plan_id=str(context.plan_id),
            plan_hash=RunnerDigest(root=context.plan_hash.root),
            payload=CapacityPlannerStartPayload(
                job_id=str(context.job_id),
                model_ref=model_ref,
                hardware_passport_artifact=self._runner_artifact(
                    context.hardware_passport_artifact,
                    locator,
                ),
                tensor_parallel_size=1,
                budget=context.budget,
            ),
        )
        return self._dispatch(request, context.job_id)

    def status(self, context: CapacityPlannerExecutionContext) -> CapacityPlannerOperation:
        request = CapacityPlannerStatusRequest(
            request_id=context.request_id,
            idempotency_key=RunnerDigest(root=context.idempotency_key.root),
            actor="autopilot-worker",
            plan_id=str(context.plan_id),
            plan_hash=RunnerDigest(root=context.plan_hash.root),
            payload=CapacityPlannerJobRefPayload(job_id=str(context.job_id)),
        )
        return self._dispatch(request, context.job_id)

    def cancel(self, context: CapacityPlannerExecutionContext) -> CapacityPlannerOperation:
        request = CancelCapacityPlannerRequest(
            request_id=context.request_id,
            idempotency_key=RunnerDigest(root=context.idempotency_key.root),
            actor="autopilot-worker",
            plan_id=str(context.plan_id),
            plan_hash=RunnerDigest(root=context.plan_hash.root),
            payload=CapacityPlannerJobRefPayload(job_id=str(context.job_id)),
        )
        return self._dispatch(request, context.job_id)

    def collect(self, context: CapacityPlannerExecutionContext) -> PlannerRawEstimate:
        request = CapacityPlannerArtifactsRequest(
            request_id=context.request_id,
            idempotency_key=RunnerDigest(root=context.idempotency_key.root),
            actor="autopilot-worker",
            plan_id=str(context.plan_id),
            plan_hash=RunnerDigest(root=context.plan_hash.root),
            payload=CapacityPlannerJobRefPayload(job_id=str(context.job_id)),
        )
        self._dispatch(request, context.job_id)
        results = self._results
        if results is None:
            raise CapacityProviderUnavailableError
        return results.read_planner_result(context.job_id)

    def _validate_bindings(
        self,
        specification: CapacityPlanningSpecification,
        context: CapacityPlannerExecutionContext,
    ) -> None:
        profile = self._profile
        if (
            profile is None
            or self._runner is None
            or self._locator is None
            or self._results is None
        ):
            raise CapacityProviderUnavailableError
        validate_capacity_specification(specification, profile)
        if specification.model_ref.type == "artifact":
            if context.model_artifact is None:
                raise CapacityProviderUnavailableError
        elif context.model_artifact is not None:
            raise CapacityProviderUnavailableError

    @staticmethod
    def _runner_artifact(
        artifact: ArtifactRef,
        locator: PlannerArtifactLocator,
    ) -> RunnerArtifactInput:
        typed = artifact
        location = locator.locate_for_runner(typed)
        return RunnerArtifactInput(
            artifact_id=str(typed.artifact_id),
            sha256=RunnerDigest(root=typed.sha256.root),
            content_type="application/json",
            size_bytes=typed.size_bytes,
            storage={"root": "output", "relative_path": location.storage_key},
        )

    def _dispatch(self, request: RunnerRequest, job_id: JobId) -> CapacityPlannerOperation:
        runner = self._runner
        if runner is None:
            raise CapacityProviderUnavailableError
        response = runner.dispatch(request)
        if not response.accepted or response.result is None:
            detail = (
                response.error.message
                if response.error is not None
                else "Host Runner rejected Planner"
            )
            raise CapacityProviderUnavailableError(detail)
        return CapacityPlannerOperation(
            job_id=job_id,
            state=response.result.state,
            provider_resource_id=response.result.resource_id,
            idempotent_replay=response.idempotent_replay,
            detail=response.result.detail,
        )
