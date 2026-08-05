"""Build verification statistics and select Champion/Fallback deterministically."""

from autopilot.capabilities.evidence.domain.enums import ChampionPolicyCode
from autopilot.capabilities.evidence.domain.errors import ChampionPolicyError
from autopilot.capabilities.evidence.domain.models import ChampionPolicy
from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.constraints import ObjectiveSpec
from autopilot.domain.identifiers import CandidateId
from autopilot.domain.provenance import DerivedProvenance
from autopilot.domain.trials import (
    ChampionSelection,
    VerificationSummary,
    calculate_verification_statistics,
)


def build_verification_summary(
    candidate_id: CandidateId,
    repeat_values: tuple[float, ...],
    repeat_constraints_satisfied: tuple[bool, ...],
    provenance: DerivedProvenance,
    policy: ChampionPolicy,
) -> VerificationSummary:
    """Derive immutable repeat statistics; callers cannot provide calculated values."""
    if (
        len(repeat_values) != policy.verification_repeats
        or len(repeat_constraints_satisfied) != policy.verification_repeats
    ):
        raise ChampionPolicyError(
            ChampionPolicyCode.INVALID_VERIFICATION_SET,
            "repeat values and constraint results must match the policy repeat count",
        )
    mean, standard_deviation, coefficient_of_variation, worst_value = (
        calculate_verification_statistics(repeat_values)
    )
    return VerificationSummary(
        candidate_id=candidate_id,
        objective=ObjectiveSpec(),
        repeat_values=repeat_values,
        mean=mean,
        standard_deviation=standard_deviation,
        coefficient_of_variation=coefficient_of_variation,
        worst_value=worst_value,
        constraints_satisfied=all(repeat_constraints_satisfied),
        provenance=provenance,
    )


def select_champion(
    verified_candidates: tuple[VerificationSummary, ...],
    selection_artifact: ArtifactRef,
    policy: ChampionPolicy,
) -> ChampionSelection:
    """Select mean-ranked Champion/Fallback only after stability and noise gates."""
    if len(verified_candidates) != policy.top_candidate_count or any(
        len(summary.repeat_values) != policy.verification_repeats
        or not summary.constraints_satisfied
        for summary in verified_candidates
    ):
        raise ChampionPolicyError(
            ChampionPolicyCode.INVALID_VERIFICATION_SET,
            "Champion selection requires three feasible candidates with complete repeats",
        )
    if any(
        summary.coefficient_of_variation > policy.max_coefficient_of_variation
        for summary in verified_candidates
    ):
        raise ChampionPolicyError(
            ChampionPolicyCode.UNSTABLE_ENVIRONMENT,
            "verification coefficient of variation exceeds the policy threshold",
        )
    ranked = sorted(
        verified_candidates,
        key=lambda summary: (
            -summary.mean,
            -summary.worst_value,
            summary.coefficient_of_variation,
            str(summary.candidate_id),
        ),
    )
    objective_delta = ranked[0].mean - ranked[1].mean
    combined_noise = policy.noise_multiplier * (
        ranked[0].standard_deviation + ranked[1].standard_deviation
    )
    relative_improvement = objective_delta / ranked[1].mean
    if (
        objective_delta <= combined_noise
        or relative_improvement < policy.minimum_relative_improvement
    ):
        raise ChampionPolicyError(
            ChampionPolicyCode.INSUFFICIENT_IMPROVEMENT,
            "top candidate improvement does not exceed the configured noise policy",
        )
    return ChampionSelection(
        champion_candidate_id=ranked[0].candidate_id,
        fallback_candidate_id=ranked[1].candidate_id,
        objective=ObjectiveSpec(),
        verified_candidates=verified_candidates,
        max_coefficient_of_variation=policy.max_coefficient_of_variation,
        noise_multiplier=policy.noise_multiplier,
        minimum_relative_improvement=policy.minimum_relative_improvement,
        selection_artifact=selection_artifact,
    )
