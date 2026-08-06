from pathlib import Path

from autopilot.capabilities.evidence.adapters.artifact_store import LocalEvidenceArtifactWriter
from autopilot.capabilities.evidence.application.bundle import (
    EvidenceBundleBuilder,
    EvidenceBundleMaterial,
)
from autopilot.capabilities.evidence.application.champion import build_verification_summary
from autopilot.capabilities.evidence.domain.models import ChampionPolicy
from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.candidates import DeploymentCandidate
from autopilot.domain.constraints import NumericConstraint
from autopilot.domain.enums import NumericMetric, NumericOperator, TrialStatus
from autopilot.domain.identifiers import (
    CandidateId,
    ExperimentId,
    PlanHash,
    PlanId,
    StudyId,
    TrialId,
)
from autopilot.domain.provenance import DerivedProvenance, MeasuredProvenance
from autopilot.domain.trials import ConstraintEvaluation, NumericMetricValue, TrialRecord
from autopilot.infrastructure.artifacts import LocalArtifactStore


def test_bundle_publishes_complete_idempotent_manifest(
    tmp_path: Path,
    evidence_candidate: DeploymentCandidate,
    capability_artifact_ref: ArtifactRef,
    capability_derived_provenance: DerivedProvenance,
) -> None:
    policy = ChampionPolicy(
        verification_repeats=2,
        max_coefficient_of_variation=0.05,
        noise_multiplier=1,
        minimum_relative_improvement=0.01,
    )
    candidates = tuple(
        DeploymentCandidate.model_validate(
            {
                **evidence_candidate.model_dump(mode="python"),
                "candidate_id": CandidateId(root=f"cand_{index + 1:032x}"),
            }
        )
        for index in range(3)
    )
    values = ((120.0, 122.0), (100.0, 102.0), (90.0, 92.0))
    summaries = tuple(
        build_verification_summary(
            candidate.candidate_id,
            repeat_values,
            (True, True),
            capability_derived_provenance,
            policy,
        )
        for candidate, repeat_values in zip(candidates, values, strict=True)
    )
    constraint = ConstraintEvaluation(
        constraint=NumericConstraint(
            metric=NumericMetric.SUCCESS_RATE,
            operator=NumericOperator.GREATER_THAN_OR_EQUAL,
            value=0.99,
        ),
        observed=NumericMetricValue(metric=NumericMetric.SUCCESS_RATE, value=1),
        passed=True,
    )
    trials = tuple(
        TrialRecord(
            trial_id=TrialId(root=f"trial_{index + 1:032x}"),
            study_id=StudyId(root="study_" + "4" * 32),
            trial_number=index,
            candidate_id=candidate.candidate_id,
            parameters=candidate.parameters,
            status=TrialStatus.COMPLETED,
            objective=NumericMetricValue(
                metric=NumericMetric.SUCCESSFUL_OUTPUT_TOKENS_PER_SECOND,
                value=values[index][0],
            ),
            constraints=(constraint,),
            provenance=MeasuredProvenance(
                provider="evalscope",
                provider_version="test",
                adapter_version="test",
                raw_artifact=capability_artifact_ref,
            ),
            evidence=(capability_artifact_ref,),
        )
        for index, candidate in enumerate(candidates)
    )
    material = EvidenceBundleMaterial(
        experiment_id=ExperimentId(root="exp_" + "5" * 32),
        plan_id=PlanId(root="plan_" + "6" * 32),
        plan_hash=PlanHash(root="sha256:" + "7" * 64),
        hardware_passport=capability_artifact_ref,
        model_profile=capability_artifact_ref,
        model_revision=capability_artifact_ref,
        engine_image_digest=capability_artifact_ref,
        requirements=capability_artifact_ref,
        workload=capability_artifact_ref,
        slo=capability_artifact_ref,
        candidates=candidates,
        trials=trials,
        summaries=summaries,
        policy=policy,
        code_revision="8" * 40,
    )
    store = LocalArtifactStore(tmp_path / "artifacts")
    builder = EvidenceBundleBuilder(LocalEvidenceArtifactWriter(store))

    first = builder.build(material)
    replay = builder.build(material)

    assert replay == first
    assert first.bundle.manifest.champion_artifact.content_type == "application/json"
    assert len(first.bundle.manifest.candidate_artifacts) == 3
    assert len(first.bundle.manifest.verification_artifacts) == 4
    assert first.champion_selection.champion_candidate_id == candidates[0].candidate_id
    manifest = store.read(
        experiment_id=material.experiment_id,
        category="evidence",
        artifact_id=first.bundle.manifest_artifact.artifact_id,
    )
    assert b'"schema_version":"evidence-bundle-manifest/v1"' in manifest
