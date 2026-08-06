"""Evidence Artifact writer backed by the existing root-confined store."""

from autopilot.domain.artifacts import ArtifactProducer, ArtifactRef
from autopilot.domain.identifiers import ArtifactId, ExperimentId
from autopilot.infrastructure.artifacts import LocalArtifactStore


class LocalEvidenceArtifactWriter:
    """Adapt :class:`LocalArtifactStore` to the Evidence capability Port."""

    def __init__(self, store: LocalArtifactStore) -> None:
        self._store = store

    def write(  # noqa: PLR0913
        self,
        *,
        experiment_id: ExperimentId,
        category: str,
        artifact_id: ArtifactId,
        data: bytes,
        content_type: str,
        producer: ArtifactProducer,
    ) -> ArtifactRef:
        return self._store.write(
            experiment_id=experiment_id,
            category=category,
            artifact_id=artifact_id,
            data=data,
            content_type=content_type,
            producer=producer,
        ).to_ref()


__all__ = ["LocalEvidenceArtifactWriter"]
