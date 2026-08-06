"""Evidence Provider port implemented by the pinned MLflow adapter."""

from typing import Protocol

from autopilot.capabilities.evidence.domain.models import (
    EvidenceRunRef,
    EvidenceRunRequest,
    EvidenceRunStatus,
    EvidenceVersionProfile,
)
from autopilot.domain.artifacts import ArtifactProducer, ArtifactRef
from autopilot.domain.identifiers import ArtifactId, ExperimentId


class EvidenceAdapter(Protocol):
    @property
    def profile(self) -> EvidenceVersionProfile: ...

    def validate(self, request: EvidenceRunRequest) -> None: ...

    def record_run(self, request: EvidenceRunRequest) -> EvidenceRunRef: ...

    def get_run_status(self, run: EvidenceRunRef) -> EvidenceRunStatus: ...


class EvidenceArtifactWriter(Protocol):
    """Publish immutable Evidence artifacts through the configured Artifact Store."""

    def write(  # noqa: PLR0913
        self,
        *,
        experiment_id: ExperimentId,
        category: str,
        artifact_id: ArtifactId,
        data: bytes,
        content_type: str,
        producer: ArtifactProducer,
    ) -> ArtifactRef: ...
