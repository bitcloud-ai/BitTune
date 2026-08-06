from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import mlflow
from mlflow import MlflowClient

from autopilot.capabilities.evidence.adapters.mlflow import MlflowTrackingAdapter
from autopilot.capabilities.evidence.domain.models import (
    EvidenceRunRequest,
    EvidenceVersionProfile,
)
from autopilot.domain.artifacts import ArtifactProducer, ArtifactRef
from autopilot.domain.candidates import DeploymentCandidate, VllmTuningSpec
from autopilot.domain.enums import Confidence, ErrorCategory, TrialStatus
from autopilot.domain.errors import DomainError, ErrorEnvelope
from autopilot.domain.identifiers import (
    ArtifactId,
    CandidateId,
    ExperimentId,
    HardwarePassportId,
    ImageDigest,
    ModelProfileId,
    ModelRevision,
    Sha256Digest,
    StudyId,
    TrialId,
)
from autopilot.domain.models import HuggingFaceModelRef
from autopilot.domain.provenance import EstimatedProvenance
from autopilot.domain.trials import TrialRecord


def _request() -> EvidenceRunRequest:
    payload = b"planner-calculation"
    artifact = ArtifactRef(
        artifact_id=ArtifactId(root="artifact_" + "1" * 32),
        sha256=Sha256Digest(root=f"sha256:{hashlib.sha256(payload).hexdigest()}"),
        content_type="application/json",
        size_bytes=len(payload),
        producer=ArtifactProducer(component="contract-test", version="1.0.0"),
    )
    candidate_id = CandidateId(root="cand_" + "2" * 32)
    candidate = DeploymentCandidate(
        candidate_id=candidate_id,
        profile="balanced",
        hardware_passport_id=HardwarePassportId(root="env_" + "3" * 32),
        hardware_passport_hash=Sha256Digest(root="sha256:" + "4" * 64),
        model_profile_id=ModelProfileId(root="model_" + "5" * 32),
        model_ref=HuggingFaceModelRef(
            repository_id="Qwen/Qwen3-8B",
            revision=ModelRevision(root="6" * 40),
        ),
        engine_image=ImageDigest(root="vllm/vllm-openai@sha256:" + "7" * 64),
        engine_version="vllm-contract-v1",
        adapter_version="deployment-adapter-contract-v1",
        workload_hash=Sha256Digest(root="sha256:" + "8" * 64),
        parameters=VllmTuningSpec(
            max_model_len=8_192,
            gpu_memory_utilization=0.9,
            max_num_seqs=8,
            max_num_batched_tokens=4_096,
            enable_chunked_prefill=True,
        ),
        estimation=EstimatedProvenance(
            provider="llm-d-planner",
            provider_version="planner-contract-v1",
            adapter_version="capacity-adapter-contract-v1",
            confidence=Confidence.MEDIUM,
            calculation_artifact=artifact,
        ),
    )
    trial = TrialRecord(
        trial_id=TrialId(root="trial_" + "9" * 32),
        study_id=StudyId(root="study_" + "a" * 32),
        trial_number=0,
        candidate_id=candidate_id,
        parameters=candidate.parameters,
        status=TrialStatus.DEPLOYMENT_FAILED,
        error=ErrorEnvelope(
            error=DomainError(
                code="DEPLOYMENT_FAILED",
                category=ErrorCategory.DEPLOYMENT_ERROR,
                message="Authorization: Bearer contract-secret must not be logged",
                retryable=False,
            )
        ),
    )
    started = datetime(2026, 8, 6, 3, 0, tzinfo=UTC)
    return EvidenceRunRequest(
        experiment_id=ExperimentId(root="exp_" + "b" * 32),
        candidate=candidate,
        trial=trial,
        code_revision="c" * 40,
        idempotency_key=Sha256Digest(root="sha256:" + "d" * 64),
        started_at=started,
        ended_at=started + timedelta(seconds=2),
    )


def test_mlflow_sdk_adapter_contract_uses_persisted_run_and_manifest(tmp_path: Path) -> None:
    db_path = (tmp_path / "mlflow.db").as_posix()
    tracking_uri = f"sqlite:///{db_path}"
    profile = EvidenceVersionProfile(
        profile_version="mlflow-contract-v1",
        provider_version=mlflow.__version__,
        adapter_version="evidence-adapter-contract-v1",
    )
    adapter = MlflowTrackingAdapter.from_tracking_uri(
        profile=profile,
        tracking_uri=tracking_uri,
    )
    request = _request()

    reference = adapter.record_run(request)
    replay = adapter.record_run(request)
    status = adapter.get_run_status(reference)
    client = MlflowClient(tracking_uri=tracking_uri)
    run = client.get_run(reference.provider_run_id)
    manifest_path = client.download_artifacts(
        reference.provider_run_id,
        "evidence/run-manifest.json",
    )

    assert replay.idempotent_replay is True
    assert status.state.value == "failed"
    assert run.info.status == "FAILED"
    assert run.data.params["engine.name"] == "vllm"
    assert "contract-secret" not in Path(manifest_path).read_text(encoding="utf-8")
