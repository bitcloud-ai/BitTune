from datetime import UTC, datetime
from unittest.mock import create_autospec

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from autopilot.domain.artifacts import ArtifactProducer
from autopilot.domain.identifiers import ArtifactId, ExperimentId, Sha256Digest
from autopilot.evidence.models import ArtifactMetadata
from autopilot.evidence.ports import ArtifactRepository
from autopilot.infrastructure.database.errors import ArtifactBindingError
from autopilot.infrastructure.database.models import ArtifactRow
from autopilot.infrastructure.database.repositories import SqlAlchemyArtifactRepository


def _metadata() -> ArtifactMetadata:
    experiment_id = ExperimentId(root=f"exp_{'1' * 32}")
    artifact_id = ArtifactId(root=f"artifact_{'2' * 32}")
    return ArtifactMetadata(
        artifact_id=artifact_id,
        experiment_id=experiment_id,
        category="benchmark",
        content_type="application/json",
        size_bytes=17,
        sha256=Sha256Digest(root=f"sha256:{'3' * 64}"),
        created_at=datetime(2026, 8, 6, 1, 2, 3, tzinfo=UTC),
        producer=ArtifactProducer(component="evalscope-adapter", version="1.0.0"),
        storage_path=(f"experiments/{experiment_id}/benchmark/{artifact_id}/payload"),
    )


def _row(metadata: ArtifactMetadata) -> ArtifactRow:
    return ArtifactRow(
        id=str(metadata.artifact_id),
        schema_version=metadata.schema_version,
        experiment_id=str(metadata.experiment_id),
        category=metadata.category,
        content_type=metadata.content_type,
        size_bytes=metadata.size_bytes,
        sha256=str(metadata.sha256),
        producer_component=metadata.producer.component,
        producer_version=metadata.producer.version,
        storage_path=metadata.storage_path,
        created_at=metadata.created_at,
    )


def test_artifact_row_columns_exactly_cover_persisted_metadata() -> None:
    assert set(ArtifactRow.__table__.columns.keys()) == {
        "id",
        "schema_version",
        "experiment_id",
        "category",
        "content_type",
        "size_bytes",
        "sha256",
        "producer_component",
        "producer_version",
        "storage_path",
        "created_at",
    }


def test_sqlalchemy_artifact_repository_round_trips_every_metadata_field() -> None:
    session = create_autospec(Session, instance=True)
    repository: ArtifactRepository = SqlAlchemyArtifactRepository(session)
    metadata = _metadata()
    session.get.return_value = None
    session.execute.return_value.scalar_one_or_none.return_value = str(metadata.artifact_id)

    repository.add(metadata)

    statement = session.execute.call_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    assert "ON CONFLICT (id) DO NOTHING" in str(compiled)
    assert compiled.params["id"] == str(metadata.artifact_id)
    assert compiled.params["schema_version"] == metadata.schema_version
    assert compiled.params["experiment_id"] == str(metadata.experiment_id)
    assert compiled.params["storage_path"] == metadata.storage_path

    row = _row(metadata)
    session.get.return_value = row
    assert repository.get(metadata.artifact_id, experiment_id=metadata.experiment_id) == metadata


def test_artifact_repository_replays_identical_metadata_but_rejects_rebinding() -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyArtifactRepository(session)
    metadata = _metadata()
    session.get.return_value = _row(metadata)

    repository.add(metadata)

    session.execute.assert_not_called()
    with pytest.raises(ArtifactBindingError):
        repository.add(metadata.model_copy(update={"content_type": "text/plain"}))


def test_artifact_repository_verifies_concurrent_insert_conflict() -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyArtifactRepository(session)
    metadata = _metadata()
    session.get.side_effect = (None, _row(metadata))
    session.execute.return_value.scalar_one_or_none.return_value = None

    repository.add(metadata)

    assert session.get.call_count == 2


def test_artifact_repository_scopes_lookup_to_experiment() -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyArtifactRepository(session)
    metadata = _metadata()
    session.get.return_value = _row(metadata)

    result = repository.get(
        metadata.artifact_id,
        experiment_id=ExperimentId(root=f"exp_{'4' * 32}"),
    )

    assert result is None
