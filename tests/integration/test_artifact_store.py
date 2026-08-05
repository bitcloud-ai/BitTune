from pathlib import Path

from autopilot.domain.artifacts import ArtifactProducer
from autopilot.domain.identifiers import ArtifactId, ExperimentId
from autopilot.infrastructure.artifacts import LocalArtifactStore


def test_artifact_remains_verifiable_after_store_reconstruction(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    experiment_id = ExperimentId.new()
    artifact_id = ArtifactId.new()
    producer = ArtifactProducer(component="evalscope-adapter", version="1.0.0")
    payload = b'{"completed":10,"failed":0}'
    first_store = LocalArtifactStore(root)

    written = first_store.write(
        experiment_id=experiment_id,
        category="benchmark",
        artifact_id=artifact_id,
        data=payload,
        content_type="application/json",
        producer=producer,
    )

    reconstructed_store = LocalArtifactStore(root)
    loaded = reconstructed_store.get_metadata(
        experiment_id=experiment_id,
        category="benchmark",
        artifact_id=artifact_id,
    )
    restored_payload = reconstructed_store.read(
        experiment_id=experiment_id,
        category="benchmark",
        artifact_id=artifact_id,
    )

    assert loaded == written
    assert restored_payload == payload
    assert not any(path.name.endswith(".tmp") for path in root.rglob("*"))
