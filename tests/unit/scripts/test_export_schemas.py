import json
from pathlib import Path

from scripts.export_schemas import SCHEMA_MODELS, export_schemas, schema_text, verify_schemas

PUBLIC_M2_SCHEMAS = {
    "benchmark-execution-specification-v1",
    "benchmark-result-v1",
    "champion-policy-v1",
    "deployment-execution-specification-v1",
    "vllm-search-space-v1",
}
INTERNAL_M2_SCHEMAS = {
    "compiled-evalscope-benchmark-v1",
    "compiled-vllm-deployment-v1",
    "deployment-preview-v1",
    "evalscope-raw-report-v1",
    "evalscope-version-profile-v1",
    "vllm-version-profile-v1",
}


def test_all_public_schemas_forbid_unknown_top_level_fields() -> None:
    for model in SCHEMA_MODELS.values():
        schema = json.loads(schema_text(model))
        assert schema["additionalProperties"] is False


def test_m2_exports_only_public_or_persistent_capability_contracts() -> None:
    assert SCHEMA_MODELS.keys() >= PUBLIC_M2_SCHEMAS
    assert SCHEMA_MODELS.keys().isdisjoint(INTERNAL_M2_SCHEMAS)


def test_exported_schemas_round_trip_through_verifier(tmp_path: Path) -> None:
    export_schemas(tmp_path)

    assert verify_schemas(tmp_path) == []
    assert len(tuple(tmp_path.glob("*.json"))) == len(SCHEMA_MODELS) + 1


def test_verifier_reports_stale_schema(tmp_path: Path) -> None:
    export_schemas(tmp_path)
    first_schema = next(tmp_path.glob("*.json"))
    first_schema.write_text("{}\n", encoding="utf-8", newline="\n")

    assert verify_schemas(tmp_path) == [f"stale generated schema: {first_schema.name}"]
