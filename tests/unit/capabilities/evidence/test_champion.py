import pytest

from autopilot.capabilities.evidence.application.champion import (
    build_verification_summary,
    select_champion,
)
from autopilot.capabilities.evidence.domain.enums import ChampionPolicyCode
from autopilot.capabilities.evidence.domain.errors import ChampionPolicyError
from autopilot.capabilities.evidence.domain.models import ChampionPolicy
from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.identifiers import CandidateId
from autopilot.domain.provenance import DerivedProvenance
from autopilot.domain.trials import VerificationSummary


def policy() -> ChampionPolicy:
    return ChampionPolicy(
        verification_repeats=2,
        max_coefficient_of_variation=0.05,
        noise_multiplier=1,
        minimum_relative_improvement=0.01,
    )


def summary(
    candidate_id: CandidateId,
    values: tuple[float, float],
    provenance: DerivedProvenance,
) -> VerificationSummary:
    return build_verification_summary(
        candidate_id,
        values,
        (True, True),
        provenance,
        policy(),
    )


def test_champion_and_fallback_are_selected_after_noise_gate(
    capability_artifact_ref: ArtifactRef,
    capability_derived_provenance: DerivedProvenance,
) -> None:
    candidates = tuple(CandidateId.new() for _ in range(3))
    verified = (
        summary(candidates[0], (120, 122), capability_derived_provenance),
        summary(candidates[1], (100, 102), capability_derived_provenance),
        summary(candidates[2], (90, 92), capability_derived_provenance),
    )

    selection = select_champion(verified, capability_artifact_ref, policy())

    assert selection.champion_candidate_id == candidates[0]
    assert selection.fallback_candidate_id == candidates[1]


def test_champion_rejects_tiny_zero_noise_improvement(
    capability_artifact_ref: ArtifactRef,
    capability_derived_provenance: DerivedProvenance,
) -> None:
    candidates = tuple(CandidateId.new() for _ in range(3))
    verified = (
        summary(candidates[0], (100.001, 100.001), capability_derived_provenance),
        summary(candidates[1], (100, 100), capability_derived_provenance),
        summary(candidates[2], (90, 90), capability_derived_provenance),
    )

    with pytest.raises(ChampionPolicyError) as caught:
        select_champion(verified, capability_artifact_ref, policy())

    assert caught.value.code is ChampionPolicyCode.INSUFFICIENT_IMPROVEMENT


def test_champion_rejects_unstable_candidate(
    capability_artifact_ref: ArtifactRef,
    capability_derived_provenance: DerivedProvenance,
) -> None:
    candidates = tuple(CandidateId.new() for _ in range(3))
    verified = (
        summary(candidates[0], (100, 120), capability_derived_provenance),
        summary(candidates[1], (80, 80), capability_derived_provenance),
        summary(candidates[2], (70, 70), capability_derived_provenance),
    )

    with pytest.raises(ChampionPolicyError) as caught:
        select_champion(verified, capability_artifact_ref, policy())

    assert caught.value.code is ChampionPolicyCode.UNSTABLE_ENVIRONMENT


def test_verification_requires_every_repeat_constraint_result(
    capability_derived_provenance: DerivedProvenance,
) -> None:
    with pytest.raises(ChampionPolicyError) as caught:
        build_verification_summary(
            CandidateId.new(),
            (100, 101),
            (True,),
            capability_derived_provenance,
            policy(),
        )

    assert caught.value.code is ChampionPolicyCode.INVALID_VERIFICATION_SET
