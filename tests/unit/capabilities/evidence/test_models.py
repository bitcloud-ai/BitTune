import pytest
from pydantic import ValidationError

from autopilot.capabilities.evidence.domain.models import EvidenceRunRequest
from autopilot.domain.identifiers import CandidateId


def test_evidence_request_requires_normalized_result_for_measured_trial(
    completed_request: EvidenceRunRequest,
) -> None:
    payload = completed_request.model_dump(mode="python")
    payload["benchmark_result"] = None

    with pytest.raises(ValidationError, match="normalized BenchmarkResult"):
        EvidenceRunRequest.model_validate(payload)


def test_evidence_request_rejects_candidate_binding_mismatch(
    failure_request: EvidenceRunRequest,
) -> None:
    payload = failure_request.model_dump(mode="python")
    payload["trial"] = {
        **payload["trial"],
        "candidate_id": CandidateId(root="cand_" + "f" * 32),
    }

    with pytest.raises(ValidationError, match="identifiers do not match"):
        EvidenceRunRequest.model_validate(payload)


def test_evidence_request_rejects_unknown_fields(
    failure_request: EvidenceRunRequest,
) -> None:
    payload = failure_request.model_dump(mode="python")
    payload["tracking_uri"] = "sqlite:///must-not-be-agent-input"

    with pytest.raises(ValidationError, match="extra_forbidden"):
        EvidenceRunRequest.model_validate(payload)
