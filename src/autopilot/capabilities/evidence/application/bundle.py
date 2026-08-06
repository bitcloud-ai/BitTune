"""Deterministic Evidence Bundle assembly over the existing Artifact Port."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import Field

from autopilot.capabilities.evidence.application.champion import select_champion
from autopilot.capabilities.evidence.domain.models import (
    ChampionPolicy,
    CodeRevision,
    EvidenceBundle,
    EvidenceBundleManifest,
)
from autopilot.capabilities.evidence.ports import EvidenceArtifactWriter
from autopilot.domain.artifacts import ArtifactProducer, ArtifactRef
from autopilot.domain.base import StrictModel
from autopilot.domain.candidates import DeploymentCandidate
from autopilot.domain.hashing import canonical_json_bytes
from autopilot.domain.identifiers import ArtifactId, ExperimentId, PlanHash, PlanId
from autopilot.domain.trials import ChampionSelection, TrialRecord, VerificationSummary

BUNDLE_PRODUCER = ArtifactProducer(component="evidence-bundle", version="m7")
JSON_CONTENT_TYPE = "application/json"
TEXT_CONTENT_TYPE = "text/plain; charset=utf-8"


class EvidenceBundleMaterial(StrictModel):
    """Trusted, already-validated inputs required to assemble a Bundle."""

    schema_version: Literal["evidence-bundle-material/v1"] = "evidence-bundle-material/v1"
    experiment_id: ExperimentId
    plan_id: PlanId
    plan_hash: PlanHash
    hardware_passport: ArtifactRef
    model_profile: ArtifactRef
    model_revision: ArtifactRef
    engine_image_digest: ArtifactRef
    requirements: ArtifactRef
    workload: ArtifactRef
    slo: ArtifactRef
    candidates: tuple[DeploymentCandidate, ...] = Field(min_length=3, max_length=480)
    trials: tuple[TrialRecord, ...] = Field(min_length=1, max_length=2_000)
    summaries: tuple[VerificationSummary, ...] = Field(min_length=3, max_length=3)
    policy: ChampionPolicy
    code_revision: CodeRevision


class EvidenceBundleResult(StrictModel):
    schema_version: Literal["evidence-bundle-result/v1"] = "evidence-bundle-result/v1"
    bundle: EvidenceBundle
    champion_selection: ChampionSelection


class ChampionSelectionInput(StrictModel):
    schema_version: Literal["champion-selection-input/v1"] = "champion-selection-input/v1"
    policy: ChampionPolicy
    summaries: tuple[VerificationSummary, ...] = Field(min_length=3, max_length=3)


class EvidenceBundleBuilder:
    """Publish every Bundle component idempotently and return only typed refs."""

    def __init__(self, writer: EvidenceArtifactWriter) -> None:
        self._writer = writer

    def build(self, material: EvidenceBundleMaterial) -> EvidenceBundleResult:
        candidate_artifacts = tuple(
            self._write_json(
                material,
                "candidates",
                index,
                candidate,
            )
            for index, candidate in enumerate(material.candidates)
        )
        trial_artifacts = tuple(
            self._write_json(material, "trials", index, trial)
            for index, trial in enumerate(material.trials)
        )
        verification_artifacts = tuple(
            self._write_json(material, "verification", index, summary)
            for index, summary in enumerate(material.summaries)
        )
        selection_input = ChampionSelectionInput(
            policy=material.policy,
            summaries=material.summaries,
        )
        selection_input_ref = self._write_bytes(
            material,
            category="verification",
            index=len(verification_artifacts),
            data=canonical_json_bytes(selection_input),
            content_type=JSON_CONTENT_TYPE,
        )
        champion_selection = select_champion(
            material.summaries,
            selection_input_ref,
            material.policy,
        )
        champion_artifact = self._write_json(
            material,
            "evidence",
            0,
            champion_selection,
        )
        report_artifact = self._write_bytes(
            material,
            category="evidence",
            index=1,
            data=_report(champion_selection, material.summaries).encode("utf-8"),
            content_type="text/markdown; charset=utf-8",
        )
        checksums_data = _checksums(
            (
                material.hardware_passport,
                material.model_profile,
                material.model_revision,
                material.engine_image_digest,
                material.requirements,
                material.workload,
                material.slo,
                *candidate_artifacts,
                *trial_artifacts,
                *verification_artifacts,
                selection_input_ref,
                champion_artifact,
                report_artifact,
            )
        )
        checksums_artifact = self._write_bytes(
            material,
            category="evidence",
            index=2,
            data=checksums_data,
            content_type=TEXT_CONTENT_TYPE,
        )
        manifest = EvidenceBundleManifest(
            experiment_id=material.experiment_id,
            plan_id=material.plan_id,
            plan_hash=material.plan_hash,
            hardware_passport=material.hardware_passport,
            model_profile=material.model_profile,
            model_revision=material.model_revision,
            engine_image_digest=material.engine_image_digest,
            requirements=material.requirements,
            workload=material.workload,
            slo=material.slo,
            candidate_artifacts=candidate_artifacts,
            trial_artifacts=trial_artifacts,
            verification_artifacts=(*verification_artifacts, selection_input_ref),
            champion_artifact=champion_artifact,
            report_artifact=report_artifact,
            checksums_artifact=checksums_artifact,
            code_revision=material.code_revision,
        )
        manifest_artifact = self._write_json(material, "evidence", 3, manifest)
        bundle = EvidenceBundle(
            manifest=manifest,
            manifest_artifact=manifest_artifact,
            champion_selection=champion_selection,
        )
        return EvidenceBundleResult(bundle=bundle, champion_selection=champion_selection)

    def _write_json(
        self,
        material: EvidenceBundleMaterial,
        category: str,
        index: int,
        model: StrictModel,
    ) -> ArtifactRef:
        return self._write_bytes(
            material,
            category=category,
            index=index,
            data=canonical_json_bytes(model),
            content_type=JSON_CONTENT_TYPE,
        )

    def _write_bytes(
        self,
        material: EvidenceBundleMaterial,
        *,
        category: str,
        index: int,
        data: bytes,
        content_type: str,
    ) -> ArtifactRef:
        digest = hashlib.sha256(
            f"bundle:{material.plan_hash}:{category}:{index}".encode()
        ).hexdigest()
        return self._writer.write(
            experiment_id=material.experiment_id,
            category=category,
            artifact_id=ArtifactId(root=f"artifact_{digest[:32]}"),
            data=data,
            content_type=content_type,
            producer=BUNDLE_PRODUCER,
        )


def _checksums(refs: tuple[ArtifactRef, ...]) -> bytes:
    return "".join(f"{ref.sha256.root}  {ref.artifact_id}\n" for ref in refs).encode("utf-8")


def _report(selection: ChampionSelection, summaries: tuple[VerificationSummary, ...]) -> str:
    lines = [
        "# Champion Verification Report",
        "",
        f"Champion: `{selection.champion_candidate_id}`",
        f"Fallback: `{selection.fallback_candidate_id}`",
        "",
        "| Candidate | Mean | CV | Worst |",
        "|---|---:|---:|---:|",
    ]
    lines.extend(
        f"| `{summary.candidate_id}` | {summary.mean:.6f} | "
        f"{summary.coefficient_of_variation:.6f} | {summary.worst_value:.6f} |"
        for summary in summaries
    )
    return "\n".join(lines) + "\n"


__all__ = ["EvidenceBundleBuilder", "EvidenceBundleMaterial", "EvidenceBundleResult"]
