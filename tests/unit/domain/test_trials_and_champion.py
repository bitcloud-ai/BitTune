import pytest
from pydantic import ValidationError

from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.candidates import VllmTuningSpec
from autopilot.domain.constraints import ObjectiveSpec
from autopilot.domain.enums import NumericMetric, TrialStatus
from autopilot.domain.identifiers import CandidateId, StudyId, TrialId
from autopilot.domain.provenance import DerivedProvenance, MeasuredProvenance
from autopilot.domain.trials import (
    ChampionSelection,
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
        coefficient_of_variation=0.004975,
        worst_value=100.0,
        constraints_satisfied=True,
        provenance=derived_provenance,
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
        provenance=measured_provenance,
        evidence=(artifact_ref,),
    )

    assert trial.status is TrialStatus.COMPLETED
