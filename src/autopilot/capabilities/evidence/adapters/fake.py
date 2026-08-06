"""Deterministic MLflow-shaped fake used by workflow and contract tests."""

from __future__ import annotations

import hashlib

from autopilot.capabilities.evidence.domain.enums import (
    EvidenceProviderState,
    EvidenceValidationCode,
)
from autopilot.capabilities.evidence.domain.errors import EvidenceProviderError
from autopilot.capabilities.evidence.domain.models import (
    EvidenceRunRef,
    EvidenceRunRequest,
    EvidenceRunStatus,
    EvidenceVersionProfile,
)
from autopilot.domain.enums import TrialStatus
from autopilot.domain.hashing import compute_content_hash


class FakeEvidenceAdapter:
    """Keep evidence lifecycle deterministic without contacting MLflow."""

    def __init__(self, profile: EvidenceVersionProfile) -> None:
        self._profile = profile
        self._runs: dict[str, EvidenceRunStatus] = {}

    @property
    def profile(self) -> EvidenceVersionProfile:
        return self._profile

    def validate(self, request: EvidenceRunRequest) -> None:
        if request.trial.status is TrialStatus.SUGGESTED:
            raise EvidenceProviderError(
                EvidenceValidationCode.PROVIDER_REJECTED,
                "non-terminal Trials cannot be recorded as evidence",
                retryable=False,
            )

    def record_run(self, request: EvidenceRunRequest) -> EvidenceRunRef:
        self.validate(request)
        request_hash = compute_content_hash(request)
        key = request.idempotency_key.root
        existing = self._runs.get(key)
        if existing is not None:
            if existing.run.request_hash != request_hash:
                raise EvidenceProviderError(
                    EvidenceValidationCode.IDEMPOTENCY_CONFLICT,
                    "the fake Tracking Run is bound to different immutable evidence",
                    retryable=False,
                )
            return existing.run.model_copy(update={"idempotent_replay": True})
        provider_run_id = "fake_" + hashlib.sha256(key.encode("ascii")).hexdigest()[:32]
        reference = EvidenceRunRef(
            provider_version=self._profile.provider_version,
            adapter_version=self._profile.adapter_version,
            provider_profile_version=self._profile.profile_version,
            provider_run_id=provider_run_id,
            experiment_id=request.experiment_id,
            trial_id=request.trial.trial_id,
            request_hash=request_hash,
        )
        state = (
            EvidenceProviderState.SUCCEEDED
            if request.trial.status in {TrialStatus.COMPLETED, TrialStatus.CONSTRAINT_FAILED}
            else EvidenceProviderState.CANCELLED
            if request.trial.status is TrialStatus.CANCELLED
            else EvidenceProviderState.FAILED
        )
        self._runs[key] = EvidenceRunStatus(
            run=reference,
            state=state,
            started_at=request.started_at,
            ended_at=request.ended_at,
        )
        return reference

    def get_run_status(self, run: EvidenceRunRef) -> EvidenceRunStatus:
        if (
            run.provider_version != self._profile.provider_version
            or run.adapter_version != self._profile.adapter_version
            or run.provider_profile_version != self._profile.profile_version
        ):
            raise EvidenceProviderError(
                EvidenceValidationCode.PROFILE_UNVERIFIED,
                "the fake Tracking Run reference does not match its profile",
                retryable=False,
            )
        for status in self._runs.values():
            if status.run.provider_run_id == run.provider_run_id:
                return status
        raise EvidenceProviderError(
            EvidenceValidationCode.RUN_NOT_FOUND,
            "the fake Tracking Run does not exist",
            retryable=False,
        )


__all__ = ["FakeEvidenceAdapter"]
