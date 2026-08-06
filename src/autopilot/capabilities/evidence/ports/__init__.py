"""Evidence Provider port implemented by the pinned MLflow adapter."""

from typing import Protocol

from autopilot.capabilities.evidence.domain.models import (
    EvidenceRunRef,
    EvidenceRunRequest,
    EvidenceRunStatus,
    EvidenceVersionProfile,
)


class EvidenceAdapter(Protocol):
    @property
    def profile(self) -> EvidenceVersionProfile: ...

    def validate(self, request: EvidenceRunRequest) -> None: ...

    def record_run(self, request: EvidenceRunRequest) -> EvidenceRunRef: ...

    def get_run_status(self, run: EvidenceRunRef) -> EvidenceRunStatus: ...
