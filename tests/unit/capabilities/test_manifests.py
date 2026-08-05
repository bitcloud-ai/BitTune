import json
import shutil
from pathlib import Path

import pytest

from scripts.validate_capability_manifests import (
    M2_CAPABILITY_SPECS,
    ManifestValidationError,
    validate_capability_package,
    validate_m2_capability_manifests,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CAPABILITIES_ROOT = REPOSITORY_ROOT / "src" / "autopilot" / "capabilities"


def test_all_m2_capability_manifests_match_code_and_layout() -> None:
    manifests = validate_m2_capability_manifests(REPOSITORY_ROOT)

    assert tuple(manifest.metadata.name for manifest in manifests) == tuple(
        spec.name for spec in M2_CAPABILITY_SPECS
    )


def test_unimplemented_runtime_surfaces_are_fail_closed() -> None:
    manifests = validate_m2_capability_manifests(REPOSITORY_ROOT)

    for manifest in manifests:
        assert manifest.implementation.deterministic_core == "implemented"
        assert manifest.implementation.agent_tools == "planned"
        assert manifest.implementation.provider_execution == "planned"
        assert manifest.provider.version_constraint is None
        assert manifest.provider.adapter_version is None
        assert manifest.provider.execution_entrypoint is None


def test_manifest_rejects_provider_drift(tmp_path: Path) -> None:
    spec = M2_CAPABILITY_SPECS[0]
    package_directory = tmp_path / spec.name
    shutil.copytree(CAPABILITIES_ROOT / spec.name, package_directory)
    manifest_path = package_directory / "manifest.yaml"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["provider"]["name"] = "evalscope"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ManifestValidationError, match="deployment Provider"):
        validate_capability_package(package_directory, spec)


def test_manifest_rejects_tool_and_schema_drift(tmp_path: Path) -> None:
    spec = M2_CAPABILITY_SPECS[1]
    package_directory = tmp_path / spec.name
    shutil.copytree(CAPABILITIES_ROOT / spec.name, package_directory)
    manifest_path = package_directory / "manifest.yaml"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["tools"] = payload["tools"][:-1]
    payload["schema_versions"] = payload["schema_versions"][:-1]
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ManifestValidationError) as caught:
        validate_capability_package(package_directory, spec)

    assert "benchmark Tool contracts" in str(caught.value)
    assert "benchmark Schema versions" in str(caught.value)


def test_manifest_rejects_missing_package_directory(tmp_path: Path) -> None:
    spec = M2_CAPABILITY_SPECS[2]
    package_directory = tmp_path / spec.name
    shutil.copytree(CAPABILITIES_ROOT / spec.name, package_directory)
    shutil.rmtree(package_directory / "tests")

    with pytest.raises(ManifestValidationError, match="missing required directory tests"):
        validate_capability_package(package_directory, spec)
