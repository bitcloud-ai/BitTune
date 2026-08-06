import json

import pytest
from pydantic import TypeAdapter, ValidationError

from runner.models import (
    RelativeStoragePath,
    RunnerRequest,
    StartDeploymentRequest,
    StorageRef,
)
from tests.unit.runner.conftest import start_deployment_data


def test_runner_request_is_discriminated_and_forbids_unknown_fields() -> None:
    adapter = TypeAdapter(RunnerRequest)
    parsed = adapter.validate_python(start_deployment_data())
    assert isinstance(parsed, StartDeploymentRequest)

    data = start_deployment_data()
    data["command"] = "docker run"
    with pytest.raises(ValidationError):
        adapter.validate_python(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("action", "docker_run"),
        ("actor", "agent"),
        ("plan_hash", "not-a-digest"),
    ],
)
def test_runner_request_rejects_unregistered_action_and_identity(field: str, value: str) -> None:
    data = start_deployment_data()
    data[field] = value
    with pytest.raises(ValidationError):
        TypeAdapter(RunnerRequest).validate_python(data)


@pytest.mark.parametrize(
    "value",
    [
        "/etc/passwd",
        r"C:\Windows\system.ini",
        "../escape",
        "models/../../escape",
        r"models\..\escape",
        "models//escape",
        "models/./escape",
    ],
)
def test_relative_storage_path_rejects_absolute_and_traversal(value: str) -> None:
    with pytest.raises(ValidationError):
        RelativeStoragePath(root=value)


def test_storage_ref_rejects_unknown_root() -> None:
    with pytest.raises(ValidationError):
        StorageRef.model_validate({"root": "host", "relative_path": "etc/passwd"})


def test_public_runner_schema_has_no_generic_execution_surface() -> None:
    schema = json.dumps(TypeAdapter(RunnerRequest).json_schema(), sort_keys=True).lower()
    forbidden_property_names = (
        '"command"',
        '"shell"',
        '"argv"',
        '"extra_args"',
        '"environment"',
        '"volumes"',
        '"host_path"',
    )
    assert all(name not in schema for name in forbidden_property_names)
