from typing import get_args

import pytest
from pydantic import TypeAdapter, ValidationError

from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.enums import Confidence, MeasurementSource
from autopilot.domain.provenance import EstimatedProvenance, Provenance


def test_artifact_ref_never_exposes_a_storage_path() -> None:
    schema_properties = ArtifactRef.model_json_schema()["properties"]

    assert "path" not in schema_properties
    assert "storage_path" not in schema_properties
    assert "uri" not in schema_properties


def test_estimated_provenance_requires_confidence_and_calculation_artifact(
    artifact_ref: ArtifactRef,
) -> None:
    provenance = EstimatedProvenance(
        provider="llm-d-planner",
        provider_version="commit-123",
        adapter_version="1.0.0",
        confidence=Confidence.MEDIUM,
        calculation_artifact=artifact_ref,
    )

    assert provenance.source is MeasurementSource.ESTIMATED


def test_provenance_discriminator_rejects_measured_data_without_raw_artifact() -> None:
    adapter = TypeAdapter(Provenance)

    with pytest.raises(ValidationError, match="raw_artifact"):
        adapter.validate_python(
            {
                "source": "measured",
                "provider": "evalscope",
                "provider_version": "1.0.0",
                "adapter_version": "1.0.0",
            }
        )


def test_provenance_union_contains_all_three_source_contracts() -> None:
    union_type = get_args(Provenance)[0]

    assert len(get_args(union_type)) == 3
