"""Application-facing persistence Port for immutable Artifact metadata."""

from typing import Protocol

from autopilot.domain.identifiers import ArtifactId, ExperimentId
from autopilot.evidence.models import ArtifactMetadata


class ArtifactRepository(Protocol):
    """Idempotently persist immutable Artifact metadata in a caller-owned transaction."""

    def add(self, metadata: ArtifactMetadata) -> None: ...

    def get(
        self,
        artifact_id: ArtifactId,
        *,
        experiment_id: ExperimentId,
    ) -> ArtifactMetadata | None: ...
