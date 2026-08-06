import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from autopilot.capabilities.benchmark.domain.models import (
    BenchmarkResult,
    LatencySummary,
    LengthSummary,
    PercentileValues,
    ReliabilitySummary,
    ThroughputSummary,
)
from autopilot.capabilities.evidence.domain.models import (
    EvidenceRunRequest,
    EvidenceVersionProfile,
)
from autopilot.domain.artifacts import ArtifactProducer, ArtifactRef
from autopilot.domain.candidates import DeploymentCandidate, VllmTuningSpec
from autopilot.domain.constraints import NumericConstraint
from autopilot.domain.enums import (
    Confidence,
    ErrorCategory,
    NumericMetric,
    NumericOperator,
    TrafficMode,
    TrialStatus,
)
from autopilot.domain.errors import DomainError, ErrorEnvelope
from autopilot.domain.identifiers import (
    ArtifactId,
    CandidateId,
    DeploymentId,
    ExperimentId,
    HardwarePassportId,
    ImageDigest,
    ModelProfileId,
    ModelRevision,
    PlanHash,
    Sha256Digest,
    StudyId,
    TrialId,
)
from autopilot.domain.models import HuggingFaceModelRef
from autopilot.domain.provenance import EstimatedProvenance, MeasuredProvenance
from autopilot.domain.trials import (
    ConstraintEvaluation,
    NumericMetricValue,
    TrialRecord,
)


def _artifact(label: bytes, suffix: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=ArtifactId(root=f"artifact_{suffix}"),
        sha256=Sha256Digest(root=f"sha256:{hashlib.sha256(label).hexdigest()}"),
        content_type="application/json",
        size_bytes=len(label),
        producer=ArtifactProducer(component="evidence-test", version="1.0.0"),
    )


@pytest.fixture
def evidence_profile() -> EvidenceVersionProfile:
    return EvidenceVersionProfile(
        profile_version="mlflow-rtx5090-test-v1",
        provider_version="3.15.1",
        adapter_version="evidence-adapter-test-v1",
    )


@pytest.fixture
def evidence_candidate(capability_artifact_ref: ArtifactRef) -> DeploymentCandidate:
    revision = ModelRevision(root="a" * 40)
    return DeploymentCandidate(
        candidate_id=CandidateId(root="cand_" + "1" * 32),
        profile="balanced",
        hardware_passport_id=HardwarePassportId(root="env_" + "2" * 32),
        hardware_passport_hash=Sha256Digest(root="sha256:" + "3" * 64),
        model_profile_id=ModelProfileId(root="model_" + "4" * 32),
        model_ref=HuggingFaceModelRef(
            repository_id="Qwen/Qwen3-8B",
            revision=revision,
        ),
        engine_image=ImageDigest(root="vllm/vllm-openai@sha256:" + "5" * 64),
        engine_version="vllm-test-v1",
        adapter_version="deployment-adapter-test-v1",
        workload_hash=Sha256Digest(root="sha256:" + "6" * 64),
        parameters=VllmTuningSpec(
            max_model_len=8_192,
            gpu_memory_utilization=0.90,
            max_num_seqs=8,
            max_num_batched_tokens=4_096,
            enable_chunked_prefill=True,
        ),
        estimation=EstimatedProvenance(
            provider="llm-d-planner",
            provider_version="planner-test-v1",
            adapter_version="capacity-adapter-test-v1",
            confidence=Confidence.MEDIUM,
            calculation_artifact=capability_artifact_ref,
        ),
    )


@pytest.fixture
def failure_request(evidence_candidate: DeploymentCandidate) -> EvidenceRunRequest:
    trial = TrialRecord(
        trial_id=TrialId(root="trial_" + "7" * 32),
        study_id=StudyId(root="study_" + "8" * 32),
        trial_number=0,
        candidate_id=evidence_candidate.candidate_id,
        parameters=evidence_candidate.parameters,
        status=TrialStatus.DEPLOYMENT_FAILED,
        error=ErrorEnvelope(
            error=DomainError(
                code="DEPLOYMENT_FAILED",
                category=ErrorCategory.DEPLOYMENT_ERROR,
                message="provider failure with Authorization: Bearer should not be persisted",
                retryable=False,
            )
        ),
    )
    started = datetime(2026, 8, 6, 2, 0, tzinfo=UTC)
    return EvidenceRunRequest(
        experiment_id=ExperimentId(root="exp_" + "9" * 32),
        candidate=evidence_candidate,
        trial=trial,
        code_revision="a" * 40,
        idempotency_key=Sha256Digest(root="sha256:" + "b" * 64),
        started_at=started,
        ended_at=started + timedelta(seconds=3),
    )


@pytest.fixture
def completed_request(
    evidence_candidate: DeploymentCandidate,
) -> EvidenceRunRequest:
    raw_artifact = _artifact(b"raw-benchmark-report", "c" * 32)
    measured = MeasuredProvenance(
        provider="evalscope",
        provider_version="evalscope-test-v1",
        adapter_version="benchmark-adapter-test-v1",
        raw_artifact=raw_artifact,
    )
    result = BenchmarkResult(
        provider_version="evalscope-test-v1",
        adapter_version="benchmark-adapter-test-v1",
        provider_profile_version="evalscope-profile-test-v1",
        provider_profile_hash=Sha256Digest(root="sha256:" + "d" * 64),
        compiled_benchmark_hash=Sha256Digest(root="sha256:" + "e" * 64),
        deployment_id=DeploymentId(root="deployment_" + "f" * 32),
        deployment_plan_hash=PlanHash(root="sha256:" + "0" * 64),
        traffic_mode=TrafficMode.BASELINE,
        scheduled_window_seconds=30,
        measurement_duration_seconds=31,
        latency=LatencySummary(
            e2e_ms=PercentileValues(p50=10, p95=20, p99=30),
            ttft_ms=PercentileValues(p50=2, p95=4, p99=6),
            tpot_ms=PercentileValues(p50=1, p95=2, p99=3),
            itl_ms=PercentileValues(p50=1, p95=2, p99=3),
        ),
        throughput=ThroughputSummary(
            requests_per_second=2,
            successful_requests_per_minute=120,
            input_tokens_per_second=200,
            successful_output_tokens_per_second=100,
            total_tokens_per_minute=18_000,
        ),
        reliability=ReliabilitySummary(
            submitted=10,
            completed=10,
            failed=0,
            timed_out=0,
            completed_within_window=10,
            success_rate=1,
            window_completion_ratio=1,
        ),
        lengths=LengthSummary(
            input_tokens=PercentileValues(p50=100, p95=100, p99=100),
            output_tokens=PercentileValues(p50=50, p95=50, p99=50),
        ),
        oom=False,
        raw_report_hash=raw_artifact.sha256,
        provenance=measured,
    )
    constraint = NumericConstraint(
        metric=NumericMetric.SUCCESS_RATE,
        operator=NumericOperator.GREATER_THAN_OR_EQUAL,
        value=0.95,
    )
    trial = TrialRecord(
        trial_id=TrialId(root="trial_" + "7" * 32),
        study_id=StudyId(root="study_" + "8" * 32),
        trial_number=0,
        candidate_id=evidence_candidate.candidate_id,
        parameters=evidence_candidate.parameters,
        status=TrialStatus.COMPLETED,
        objective=NumericMetricValue(
            metric=NumericMetric.SUCCESSFUL_OUTPUT_TOKENS_PER_SECOND,
            value=100,
        ),
        constraints=(
            ConstraintEvaluation(
                constraint=constraint,
                observed=NumericMetricValue(
                    metric=NumericMetric.SUCCESS_RATE,
                    value=1,
                ),
                passed=True,
            ),
        ),
        provenance=measured,
        evidence=(raw_artifact,),
    )
    started = datetime(2026, 8, 6, 2, 0, tzinfo=UTC)
    return EvidenceRunRequest(
        experiment_id=ExperimentId(root="exp_" + "9" * 32),
        candidate=evidence_candidate,
        trial=trial,
        benchmark_result=result,
        code_revision="a" * 40,
        idempotency_key=Sha256Digest(root="sha256:" + "b" * 64),
        started_at=started,
        ended_at=started + timedelta(seconds=31),
    )
