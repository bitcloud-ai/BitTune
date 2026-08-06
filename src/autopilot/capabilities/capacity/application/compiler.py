"""Pure conversion of a closed Planner estimate into Capacity and Candidates."""

from __future__ import annotations

import hashlib

from autopilot.capabilities.capacity.application.validator import hardware_passport_hash
from autopilot.capabilities.capacity.domain.enums import CandidateProfile, CapacityValidationCode
from autopilot.capabilities.capacity.domain.errors import CapacityValidationError
from autopilot.capabilities.capacity.domain.models import (
    CapacityEstimate,
    CapacityPlan,
    CapacityPlanMaterial,
    CapacityPlannerVersionProfile,
    CapacityPlanningSpecification,
    PlannerRawEstimate,
)
from autopilot.domain.candidates import DeploymentCandidate, VllmTuningSpec
from autopilot.domain.enums import Confidence
from autopilot.domain.hashing import compute_content_hash
from autopilot.domain.identifiers import CandidateId, PlanHash, Sha256Digest
from autopilot.domain.models import ModelProfile
from autopilot.domain.provenance import EstimatedProvenance


def _candidate_id(
    profile: CandidateProfile,
    specification: CapacityPlanningSpecification,
) -> CandidateId:
    material = "|".join(
        (
            profile.value,
            str(specification.hardware_passport.hardware_passport_id),
            str(specification.model_ref),
            str(specification.workload.model_dump_json()),
        )
    ).encode("utf-8")
    return CandidateId(root=f"cand_{hashlib.sha256(material).hexdigest()[:32]}")


def _candidate_parameters(
    profile: CandidateProfile,
    specification: CapacityPlanningSpecification,
    estimate: CapacityEstimate,
) -> VllmTuningSpec:
    max_model_len = min(
        specification.requested_max_model_len,
        estimate.limits.estimated_max_context_tokens,
    )
    max_model_len = max(
        specification.workload.prompt_tokens + specification.workload.output_tokens,
        min(max_model_len, 50_000_000),
    )
    if profile is CandidateProfile.CONSERVATIVE:
        utilization, sequences, batch = 0.84, 4, 2_048
    elif profile is CandidateProfile.BALANCED:
        utilization, sequences, batch = (
            specification.requested_gpu_memory_utilization,
            min(8, max(4, estimate.limits.estimated_concurrency)),
            4_096,
        )
    else:
        utilization, sequences, batch = 0.94, 16, 8_192
    allowed_sequences = (4, 8, 16, 32)
    allowed_batches = (2_048, 4_096, 8_192, 16_384)
    return VllmTuningSpec(
        max_model_len=max_model_len,
        gpu_memory_utilization=utilization,
        max_num_seqs=next(value for value in allowed_sequences if value >= sequences),
        max_num_batched_tokens=next(value for value in allowed_batches if value >= batch),
        enable_chunked_prefill=True,
        trust_remote_code=False,
    )


def _build_candidate(
    profile: CandidateProfile,
    specification: CapacityPlanningSpecification,
    planner_profile: CapacityPlannerVersionProfile,
    estimate: CapacityEstimate,
    model_profile: ModelProfile,
) -> DeploymentCandidate:
    parameters = _candidate_parameters(profile, specification, estimate)
    return DeploymentCandidate(
        candidate_id=_candidate_id(profile, specification),
        profile=profile.value,
        hardware_passport_id=specification.hardware_passport.hardware_passport_id,
        hardware_passport_hash=Sha256Digest(root=hardware_passport_hash(specification)),
        model_profile_id=model_profile.model_profile_id,
        model_ref=model_profile.model_ref,
        engine_image=planner_profile.candidate_engine_image,
        engine_version=planner_profile.candidate_engine_version,
        adapter_version=planner_profile.deployment_adapter_version,
        workload_hash=compute_content_hash(specification.workload),
        parameters=parameters,
        estimation=estimate.provenance,
    )


def compile_capacity_plan(
    specification: CapacityPlanningSpecification,
    planner_profile: CapacityPlannerVersionProfile,
    raw_estimate: PlannerRawEstimate,
) -> CapacityPlan:
    """Compile a validated Planner DTO without provider I/O."""
    if raw_estimate.model_profile.model_ref != specification.model_ref:
        raise CapacityValidationError(
            CapacityValidationCode.MODEL_UNSUPPORTED,
            "Planner model profile does not match the requested model revision",
            "model_ref",
        )
    artifact = raw_estimate.calculation_artifact
    provenance = EstimatedProvenance(
        provider="llm-d-planner",
        provider_version=planner_profile.provider_version,
        adapter_version=planner_profile.adapter_version,
        confidence=Confidence.MEDIUM,
        calculation_artifact=artifact,
    )
    estimate = CapacityEstimate(
        fit=raw_estimate.fit,
        confidence="medium",
        memory=raw_estimate.memory,
        limits=raw_estimate.limits,
        provenance=provenance,
    )
    candidates: tuple[DeploymentCandidate, ...] = ()
    if estimate.fit:
        candidates = tuple(
            _build_candidate(
                candidate_profile,
                specification,
                planner_profile,
                estimate,
                raw_estimate.model_profile,
            )
            for candidate_profile in (
                CandidateProfile.CONSERVATIVE,
                CandidateProfile.BALANCED,
                CandidateProfile.THROUGHPUT,
            )
        )
    material = CapacityPlanMaterial(
        provider_version=planner_profile.provider_version,
        adapter_version=planner_profile.adapter_version,
        provider_profile_version=planner_profile.profile_version,
        hardware_passport_hash=Sha256Digest(root=hardware_passport_hash(specification)),
        model_profile=raw_estimate.model_profile,
        workload_hash=compute_content_hash(specification.workload),
        estimate=estimate,
        candidates=candidates,
    )
    return CapacityPlan(
        **material.model_dump(exclude={"schema_version"}),
        plan_hash=PlanHash(root=compute_content_hash(material).root),
    )
