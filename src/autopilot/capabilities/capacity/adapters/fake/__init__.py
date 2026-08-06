"""Deterministic Planner fake used by non-GPU workflow tests."""

from __future__ import annotations

import hashlib

from autopilot.capabilities.capacity.application.service import CapacityPlanningService
from autopilot.capabilities.capacity.domain.models import (
    CapacityLimits,
    CapacityMemoryEstimate,
    CapacityPlan,
    CapacityPlannerVersionProfile,
    CapacityPlanningSpecification,
    PlannerRawEstimate,
)
from autopilot.domain.artifacts import ArtifactProducer, ArtifactRef
from autopilot.domain.enums import Confidence
from autopilot.domain.hashing import compute_content_hash
from autopilot.domain.identifiers import (
    ArtifactId,
    ImageDigest,
    ModelProfileId,
    Sha256Digest,
)
from autopilot.domain.models import ModelProfile
from autopilot.domain.provenance import EstimatedProvenance

FAKE_CAPACITY_PROFILE = CapacityPlannerVersionProfile(
    profile_version="fake-llmd-capacity-v1",
    provider_version="fake-llmd-1.0.0",
    adapter_version="fake-capacity-adapter-v1",
    planner_image=ImageDigest(
        root="llm-d/planner@sha256:" + "1" * 64,
    ),
    rtx_5090_verified=True,
    candidate_engine_version="fake-vllm-1.0.0",
    candidate_engine_image=ImageDigest(
        root="vllm/vllm-openai@sha256:" + "2" * 64,
    ),
    deployment_adapter_version="fake-deployment-adapter-v1",
    memory_safety_margin_ratio=0.10,
)


def _artifact(payload: bytes, *, component: str, version: str) -> ArtifactRef:
    digest = hashlib.sha256(payload).hexdigest()
    return ArtifactRef(
        artifact_id=ArtifactId(root=f"artifact_{digest[:32]}"),
        sha256=Sha256Digest(root=f"sha256:{digest}"),
        content_type="application/json",
        size_bytes=len(payload),
        producer=ArtifactProducer(component=component, version=version),
    )


class FakePlannerExecutionClient:
    """Return a fixed, plausible estimate without reading model files or GPU state."""

    def estimate(
        self,
        specification: CapacityPlanningSpecification,
        profile: CapacityPlannerVersionProfile,
    ) -> PlannerRawEstimate:
        config_artifact = _artifact(
            specification.model_ref.model_dump_json().encode("utf-8"),
            component="model-profile",
            version=profile.adapter_version,
        )
        model_profile = ModelProfile(
            model_profile_id=ModelProfileId(
                root=f"model_{compute_content_hash(specification.model_ref).root.removeprefix('sha256:')[:32]}"
            ),
            model_ref=specification.model_ref,
            architectures=("Qwen3ForCausalLM",),
            parameter_count=8_000_000_000,
            layer_count=36,
            hidden_size=4_096,
            attention_heads=32,
            kv_heads=8,
            max_context_tokens=specification.requested_max_model_len,
            quantization=None,
            license_id="apache-2.0",
            config_artifact=config_artifact,
            provenance=EstimatedProvenance(
                provider="llm-d-planner",
                provider_version=profile.provider_version,
                adapter_version=profile.adapter_version,
                confidence=Confidence.MEDIUM,
                calculation_artifact=config_artifact,
            ),
            captured_at=specification.hardware_passport.captured_at,
        )
        total = 8_000_000_000 + specification.workload.prompt_tokens * 2_048
        total += specification.workload.output_tokens * specification.expected_concurrency * 2_048
        total = int(total * (1 + profile.memory_safety_margin_ratio))
        raw_payload = model_profile.model_dump_json().encode("utf-8")
        calculation_artifact = _artifact(
            raw_payload,
            component="capacity",
            version=profile.adapter_version,
        )
        available = specification.hardware_passport.accelerators[0].memory_free_bytes
        return PlannerRawEstimate(
            fit=total <= available,
            model_profile=model_profile,
            memory=CapacityMemoryEstimate(
                weights_bytes=8_000_000_000,
                kv_cache_budget_bytes=specification.workload.output_tokens
                * specification.expected_concurrency
                * 2_048,
                activation_bytes=specification.workload.prompt_tokens * 2_048,
                overhead_bytes=total
                - 8_000_000_000
                - specification.workload.output_tokens * specification.expected_concurrency * 2_048
                - specification.workload.prompt_tokens * 2_048,
                total_required_bytes=total,
            ),
            limits=CapacityLimits(
                estimated_max_context_tokens=specification.requested_max_model_len,
                estimated_concurrency=specification.expected_concurrency,
            ),
            calculation_artifact=calculation_artifact,
        )


class FakeCapacityPlannerAdapter:
    """Adapter facade with the same typed lifecycle as the real Planner."""

    def __init__(self) -> None:
        self._service = CapacityPlanningService(
            profile=FAKE_CAPACITY_PROFILE,
            client=FakePlannerExecutionClient(),
        )

    @property
    def profile(self) -> CapacityPlannerVersionProfile:
        return self._service.profile

    def validate(self, specification: CapacityPlanningSpecification) -> None:
        self._service.validate(specification)

    def create_plan(self, specification: CapacityPlanningSpecification) -> CapacityPlan:
        return self._service.create_plan(specification)
