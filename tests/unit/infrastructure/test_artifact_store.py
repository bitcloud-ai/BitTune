import hashlib
import json
from pathlib import Path, PurePosixPath

import pytest

import autopilot.infrastructure.artifacts as artifact_module
from autopilot.domain.artifacts import ArtifactProducer
from autopilot.domain.identifiers import ArtifactId, ExperimentId
from autopilot.infrastructure.artifacts import (
    ArtifactAlreadyExistsError,
    ArtifactInputError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactPathError,
    ArtifactStorageError,
    ArtifactStoreErrorCode,
    LocalArtifactStore,
)

_SIMULATED_PUBLISH_FAILURE = "simulated publish failure"


@pytest.fixture
def experiment_id() -> ExperimentId:
    return ExperimentId(root=f"exp_{'1' * 32}")


@pytest.fixture
def artifact_id() -> ArtifactId:
    return ArtifactId(root=f"artifact_{'2' * 32}")


@pytest.fixture
def producer() -> ArtifactProducer:
    return ArtifactProducer(component="unit-test", version="1.0.0")


def _write_artifact(
    store: LocalArtifactStore,
    experiment_id: ExperimentId,
    artifact_id: ArtifactId,
    producer: ArtifactProducer,
    data: bytes = b'{"status":"ok"}',
):
    return store.write(
        experiment_id=experiment_id,
        category="benchmark",
        artifact_id=artifact_id,
        data=data,
        content_type="application/json",
        producer=producer,
    )


def _payload_path(store: LocalArtifactStore, storage_path: str) -> Path:
    return store.root.joinpath(*PurePosixPath(storage_path).parts)


def _symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable in this test environment: {error}")


def test_write_persists_hash_metadata_and_agent_safe_reference(
    tmp_path: Path,
    experiment_id: ExperimentId,
    artifact_id: ArtifactId,
    producer: ArtifactProducer,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    data = b'{"status":"ok"}'

    metadata = _write_artifact(store, experiment_id, artifact_id, producer, data)

    expected_digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
    assert metadata.sha256.root == expected_digest
    assert metadata.size_bytes == len(data)
    assert metadata.content_type == "application/json"
    assert metadata.producer == producer
    assert metadata.storage_path == (f"experiments/{experiment_id}/benchmark/{artifact_id}/payload")
    assert metadata.uri == f"artifact://experiments/{experiment_id}/benchmark/{artifact_id}"
    assert metadata.to_ref().model_dump(mode="json") == {
        "schema_version": "artifact-ref/v1",
        "artifact_id": str(artifact_id),
        "sha256": expected_digest,
        "content_type": "application/json",
        "size_bytes": len(data),
        "producer": {"component": "unit-test", "version": "1.0.0"},
    }
    assert (
        store.read(
            experiment_id=experiment_id,
            category="benchmark",
            artifact_id=artifact_id,
        )
        == data
    )


@pytest.mark.parametrize(
    "category",
    ["../outside", "/absolute", r"C:\outside", "a/b", r"a\b", ".", "..", "con"],
)
def test_write_rejects_unsafe_or_reserved_categories(
    tmp_path: Path,
    experiment_id: ExperimentId,
    artifact_id: ArtifactId,
    producer: ArtifactProducer,
    category: str,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")

    with pytest.raises(ArtifactInputError) as caught:
        store.write(
            experiment_id=experiment_id,
            category=category,
            artifact_id=artifact_id,
            data=b"payload",
            content_type="application/octet-stream",
            producer=producer,
        )

    assert caught.value.code is ArtifactStoreErrorCode.INVALID_INPUT


def test_write_rejects_untyped_identifiers_and_non_bytes(
    tmp_path: Path,
    experiment_id: ExperimentId,
    artifact_id: ArtifactId,
    producer: ArtifactProducer,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")

    with pytest.raises(ArtifactInputError):
        store.write(
            experiment_id=str(experiment_id),  # type: ignore[arg-type]
            category="benchmark",
            artifact_id=artifact_id,
            data=b"payload",
            content_type="application/octet-stream",
            producer=producer,
        )
    with pytest.raises(ArtifactInputError):
        store.write(
            experiment_id=experiment_id,
            category="benchmark",
            artifact_id=artifact_id,
            data=bytearray(b"payload"),  # type: ignore[arg-type]
            content_type="application/octet-stream",
            producer=producer,
        )


def test_write_never_overwrites_an_existing_artifact(
    tmp_path: Path,
    experiment_id: ExperimentId,
    artifact_id: ArtifactId,
    producer: ArtifactProducer,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    _write_artifact(store, experiment_id, artifact_id, producer)

    with pytest.raises(ArtifactAlreadyExistsError) as caught:
        _write_artifact(store, experiment_id, artifact_id, producer, b"replacement")

    assert caught.value.code is ArtifactStoreErrorCode.ALREADY_EXISTS


def test_write_idempotently_replays_identical_artifact(
    tmp_path: Path,
    experiment_id: ExperimentId,
    artifact_id: ArtifactId,
    producer: ArtifactProducer,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    first = _write_artifact(store, experiment_id, artifact_id, producer)

    replayed = _write_artifact(store, experiment_id, artifact_id, producer)

    assert replayed == first
    assert (
        store.read(
            experiment_id=experiment_id,
            category="benchmark",
            artifact_id=artifact_id,
        )
        == b'{"status":"ok"}'
    )


def test_read_rejects_payload_hash_or_size_mismatch(
    tmp_path: Path,
    experiment_id: ExperimentId,
    artifact_id: ArtifactId,
    producer: ArtifactProducer,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    metadata = _write_artifact(store, experiment_id, artifact_id, producer)
    _payload_path(store, metadata.storage_path).write_bytes(b"tampered")

    with pytest.raises(ArtifactIntegrityError) as caught:
        store.read(
            experiment_id=experiment_id,
            category="benchmark",
            artifact_id=artifact_id,
        )

    assert caught.value.code is ArtifactStoreErrorCode.INTEGRITY_ERROR


def test_read_rejects_tampered_relative_storage_path(
    tmp_path: Path,
    experiment_id: ExperimentId,
    artifact_id: ArtifactId,
    producer: ArtifactProducer,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    metadata = _write_artifact(store, experiment_id, artifact_id, producer)
    metadata_path = _payload_path(store, metadata.storage_path).parent / "metadata.json"
    document = json.loads(metadata_path.read_text(encoding="utf-8"))
    document["storage_path"] = "../../outside"
    metadata_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ArtifactIntegrityError):
        store.read(
            experiment_id=experiment_id,
            category="benchmark",
            artifact_id=artifact_id,
        )


def test_missing_artifact_has_a_typed_not_found_error(
    tmp_path: Path,
    experiment_id: ExperimentId,
    artifact_id: ArtifactId,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")

    with pytest.raises(ArtifactNotFoundError) as caught:
        store.read(
            experiment_id=experiment_id,
            category="benchmark",
            artifact_id=artifact_id,
        )

    assert caught.value.code is ArtifactStoreErrorCode.NOT_FOUND


def test_write_rejects_a_symlinked_experiments_directory(
    tmp_path: Path,
    experiment_id: ExperimentId,
    artifact_id: ArtifactId,
    producer: ArtifactProducer,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    experiments_directory = store.root / "experiments"
    experiments_directory.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _symlink_or_skip(experiments_directory, outside, target_is_directory=True)

    with pytest.raises(ArtifactPathError) as caught:
        _write_artifact(store, experiment_id, artifact_id, producer)

    assert caught.value.code is ArtifactStoreErrorCode.UNSAFE_PATH
    assert not any(outside.iterdir())


def test_read_rejects_a_payload_symlink_escape(
    tmp_path: Path,
    experiment_id: ExperimentId,
    artifact_id: ArtifactId,
    producer: ArtifactProducer,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    metadata = _write_artifact(store, experiment_id, artifact_id, producer)
    payload_path = _payload_path(store, metadata.storage_path)
    payload_path.unlink()
    outside = tmp_path / "outside-payload"
    outside.write_bytes(b'{"status":"ok"}')
    _symlink_or_skip(payload_path, outside, target_is_directory=False)

    with pytest.raises(ArtifactPathError):
        store.read(
            experiment_id=experiment_id,
            category="benchmark",
            artifact_id=artifact_id,
        )


def test_failed_publish_removes_temporary_data_without_exposing_artifact(
    tmp_path: Path,
    experiment_id: ExperimentId,
    artifact_id: ArtifactId,
    producer: ArtifactProducer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")

    def fail_publish(_source: Path, _destination: Path) -> None:
        raise OSError(_SIMULATED_PUBLISH_FAILURE)

    monkeypatch.setattr(artifact_module.Path, "rename", fail_publish)

    with pytest.raises(ArtifactStorageError) as caught:
        _write_artifact(store, experiment_id, artifact_id, producer)

    category_directory = store.root / "experiments" / str(experiment_id) / "benchmark"
    assert caught.value.code is ArtifactStoreErrorCode.STORAGE_ERROR
    assert list(category_directory.iterdir()) == []
