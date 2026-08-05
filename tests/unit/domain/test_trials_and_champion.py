import pytest
from pydantic import ValidationError

from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.candidates import VllmTuningSpec
from autopilot.domain.constraints import NumericConstraint, ObjectiveSpec
from autopilot.domain.enums import NumericMetric, NumericOperator, TrialStatus
from autopilot.domain.errors import ErrorEnvelope
from autopilot.domain.identifiers import CandidateId, StudyId, TrialId
from autopilot.domain.provenance import DerivedProvenance, MeasuredProvenance
from autopilot.domain.trials import (
    ChampionSelection,
    ConstraintEvaluation,
    NumericMetricValue,
    TrialRecord,
    VerificationSummary,
)


def tuning_spec() -> VllmTuningSpec:
    return VllmTuningSpec(
        max_model_len=8_192,
        gpu_memory_utilization=0.90,
        max_num_seqs=8,
        max_num_batched_tokens=4_096,
        enable_chunked_prefill=True,
    )


def verification(
    candidate_id: CandidateId,
    derived_provenance: DerivedProvenance,
) -> VerificationSummary:
    return VerificationSummary(
        candidate_id=candidate_id,
        objective=ObjectiveSpec(),
        repeat_values=(100.0, 101.0),
        mean=100.5,
        standard_deviation=0.5,
        coefficient_of_variation=0.5 / 100.5,
        worst_value=100.0,
        constraints_satisfied=True,
        provenance=derived_provenance,
    )


def passing_constraint() -> ConstraintEvaluation:
    constraint = NumericConstraint(
        metric=NumericMetric.SUCCESS_RATE,
        operator=NumericOperator.GREATER_THAN_OR_EQUAL,
        value=1,
    )
    return ConstraintEvaluation(
        constraint=constraint,
        observed={
            "kind": "numeric",
            "metric": NumericMetric.SUCCESS_RATE,
            "value": 1,
        },
        passed=True,
    )


def test_completed_trial_requires_measured_evidence(
    measured_provenance: MeasuredProvenance,
) -> None:
    with pytest.raises(ValidationError, match="objective, provenance, and evidence"):
        TrialRecord(
            trial_id=TrialId.new(),
            study_id=StudyId.new(),
            trial_number=0,
            candidate_id=CandidateId.new(),
            parameters=tuning_spec(),
            status=TrialStatus.COMPLETED,
            provenance=measured_provenance,
        )


def test_champion_and_fallback_must_be_distinct_verified_candidates(
    artifact_ref: ArtifactRef,
    derived_provenance: DerivedProvenance,
) -> None:
    candidate = CandidateId.new()

    with pytest.raises(ValidationError, match="must be distinct"):
        second = CandidateId.new()
        third = CandidateId.new()
        ChampionSelection(
            champion_candidate_id=candidate,
            fallback_candidate_id=candidate,
            objective=ObjectiveSpec(),
            verified_candidates=(
                verification(candidate, derived_provenance),
                verification(second, derived_provenance),
                verification(third, derived_provenance),
            ),
            max_coefficient_of_variation=0.05,
            noise_multiplier=1,
            minimum_relative_improvement=0.01,
            selection_artifact=artifact_ref,
        )


def test_completed_trial_accepts_typed_objective_and_evidence(
    artifact_ref: ArtifactRef,
    measured_provenance: MeasuredProvenance,
) -> None:
    trial = TrialRecord(
        trial_id=TrialId.new(),
        study_id=StudyId.new(),
        trial_number=0,
        candidate_id=CandidateId.new(),
        parameters=tuning_spec(),
        status=TrialStatus.COMPLETED,
        objective=NumericMetricValue(
            metric=NumericMetric.SUCCESSFUL_OUTPUT_TOKENS_PER_SECOND,
            value=100,
        ),
        constraints=(passing_constraint(),),
        provenance=measured_provenance,
        evidence=(artifact_ref,),
    )

    assert trial.status is TrialStatus.COMPLETED


@pytest.mark.parametrize(
    "status",
    [
        TrialStatus.REJECTED_STATIC,
        TrialStatus.DEPLOYMENT_FAILED,
        TrialStatus.BENCHMARK_FAILED,
        TrialStatus.OOM,
    ],
)
def test_failed_trial_requires_typed_error(status: TrialStatus) -> None:
    with pytest.raises(ValidationError, match="typed error"):
        TrialRecord(
            trial_id=TrialId.new(),
            study_id=StudyId.new(),
            trial_number=0,
            candidate_id=CandidateId.new(),
            parameters=tuning_spec(),
            status=status,
        )


def test_non_measured_trial_rejects_measured_data(
    artifact_ref: ArtifactRef,
    measured_provenance: MeasuredProvenance,
) -> None:
    with pytest.raises(ValidationError, match="lifecycle status"):
        TrialRecord(
            trial_id=TrialId.new(),
            study_id=StudyId.new(),
            trial_number=0,
            candidate_id=CandidateId.new(),
            parameters=tuning_spec(),
            status=TrialStatus.SUGGESTED,
            objective=NumericMetricValue(
                metric=NumericMetric.SUCCESSFUL_OUTPUT_TOKENS_PER_SECOND,
                value=100,
            ),
            provenance=measured_provenance,
            evidence=(artifact_ref,),
        )


def test_completed_trial_rejects_error(
    artifact_ref: ArtifactRef,
    measured_provenance: MeasuredProvenance,
    error_envelope: ErrorEnvelope,
) -> None:
    with pytest.raises(ValidationError, match="lifecycle"):
        TrialRecord(
            trial_id=TrialId.new(),
            study_id=StudyId.new(),
            trial_number=0,
            candidate_id=CandidateId.new(),
            parameters=tuning_spec(),
            status=TrialStatus.COMPLETED,
            objective=NumericMetricValue(
                metric=NumericMetric.SUCCESSFUL_OUTPUT_TOKENS_PER_SECOND,
                value=100,
            ),
            constraints=(passing_constraint(),),
            provenance=measured_provenance,
            evidence=(artifact_ref,),
            error=error_envelope,
        )


def test_verification_rejects_forged_statistics(
    derived_provenance: DerivedProvenance,
) -> None:
    with pytest.raises(ValidationError, match="statistics"):
        VerificationSummary(
            candidate_id=CandidateId.new(),
            objective=ObjectiveSpec(),
            repeat_values=(100.0, 101.0),
            mean=999.0,
            standard_deviation=0.5,
            coefficient_of_variation=0.5 / 100.5,
            worst_value=100.0,
            constraints_satisfied=True,
            provenance=derived_provenance,
        )


def test_verification_statistics_remain_typed_at_large_finite_values(
    derived_provenance: DerivedProvenance,
) -> None:
    summary = VerificationSummary(
        candidate_id=CandidateId.new(),
        objective=ObjectiveSpec(),
        repeat_values=(1e308, 1e308),
        mean=1e308,
        standard_deviation=0,
        coefficient_of_variation=0,
        worst_value=1e308,
        constraints_satisfied=True,
        provenance=derived_provenance,
    )

    assert summary.mean == 1e308


def test_verification_rejects_large_relative_error_at_small_values(
    derived_provenance: DerivedProvenance,
) -> None:
    with pytest.raises(ValidationError, match="statistics"):
        VerificationSummary(
            candidate_id=CandidateId.new(),
            objective=ObjectiveSpec(),
            repeat_values=(1e-12, 1e-12),
            mean=5e-10,
            standard_deviation=0,
            coefficient_of_variation=0,
            worst_value=5e-10,
            constraints_satisfied=True,
            provenance=derived_provenance,
        )


def test_champion_selection_enforces_ranking_and_stability(
    artifact_ref: ArtifactRef,
    derived_provenance: DerivedProvenance,
) -> None:
    first = CandidateId.new()
    second = CandidateId.new()
    third = CandidateId.new()

    def stable_summary(candidate_id: CandidateId, value: float) -> VerificationSummary:
        return VerificationSummary(
            candidate_id=candidate_id,
            objective=ObjectiveSpec(),
            repeat_values=(value, value),
            mean=value,
            standard_deviation=0,
            coefficient_of_variation=0,
            worst_value=value,
            constraints_satisfied=True,
            provenance=derived_provenance,
        )

    summaries = (
        stable_summary(first, 120),
        stable_summary(second, 110),
        stable_summary(third, 100),
    )
    selection = ChampionSelection(
        champion_candidate_id=first,
        fallback_candidate_id=second,
        objective=ObjectiveSpec(),
        verified_candidates=summaries,
        max_coefficient_of_variation=0.05,
        noise_multiplier=1,
        minimum_relative_improvement=0.01,
        selection_artifact=artifact_ref,
    )

    assert selection.champion_candidate_id == first

    with pytest.raises(ValidationError, match="must be distinct"):
        ChampionSelection(
            champion_candidate_id=second,
            fallback_candidate_id=first,
            objective=ObjectiveSpec(),
            verified_candidates=summaries,
            max_coefficient_of_variation=0.05,
            noise_multiplier=1,
            minimum_relative_improvement=0.01,
            selection_artifact=artifact_ref,
        )

    unstable = VerificationSummary(
        candidate_id=first,
        objective=ObjectiveSpec(),
        repeat_values=(100, 120),
        mean=110,
        standard_deviation=10,
        coefficient_of_variation=10 / 110,
        worst_value=100,
        constraints_satisfied=True,
        provenance=derived_provenance,
    )
    with pytest.raises(ValidationError, match="variation"):
        ChampionSelection(
            champion_candidate_id=first,
            fallback_candidate_id=second,
            objective=ObjectiveSpec(),
            verified_candidates=(unstable, summaries[1], summaries[2]),
            max_coefficient_of_variation=0.05,
            noise_multiplier=1,
            minimum_relative_improvement=0.01,
            selection_artifact=artifact_ref,
        )


def test_champion_rejects_improvement_below_noise_policy(
    artifact_ref: ArtifactRef,
    derived_provenance: DerivedProvenance,
) -> None:
    candidate_ids = (CandidateId.new(), CandidateId.new(), CandidateId.new())

    def summary(candidate_id: CandidateId, value: float) -> VerificationSummary:
        return VerificationSummary(
            candidate_id=candidate_id,
            objective=ObjectiveSpec(),
            repeat_values=(value, value),
            mean=value,
            standard_deviation=0,
            coefficient_of_variation=0,
            worst_value=value,
            constraints_satisfied=True,
            provenance=derived_provenance,
        )

    summaries = (
        summary(candidate_ids[0], 100.001),
        summary(candidate_ids[1], 100.0),
        summary(candidate_ids[2], 90.0),
    )

    with pytest.raises(ValidationError, match="noise policy"):
        ChampionSelection(
            champion_candidate_id=candidate_ids[0],
            fallback_candidate_id=candidate_ids[1],
            objective=ObjectiveSpec(),
            verified_candidates=summaries,
            max_coefficient_of_variation=0.05,
            noise_multiplier=1,
            minimum_relative_improvement=0.01,
            selection_artifact=artifact_ref,
        )
