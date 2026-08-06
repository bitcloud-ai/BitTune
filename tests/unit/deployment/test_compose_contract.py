from pathlib import Path

COMPOSE = Path(__file__).parents[2] / ".." / "deploy" / "compose.yaml"


def test_control_plane_compose_requires_immutable_images_and_api_hardening() -> None:
    document = COMPOSE.resolve().read_text(encoding="utf-8")

    assert ":latest" not in document
    assert "${AUTOPILOT_API_IMAGE:?" in document
    assert "${AUTOPILOT_POSTGRES_IMAGE:?" in document
    assert "${AUTOPILOT_OPA_IMAGE:?" in document
    assert "${AUTOPILOT_MLFLOW_IMAGE:?" in document

    api = document.split("  autopilot-api:\n", maxsplit=1)[1]
    assert 'user: "65532:65532"' in api
    assert "read_only: true" in api
    assert 'cap_drop: ["ALL"]' in api
    assert "no-new-privileges:true" in api
    assert "docker.sock" not in api
    assert "gpu" not in api.casefold()


def test_deployment_template_does_not_embed_provider_secret_values() -> None:
    env_template = (COMPOSE.parent / ".env.example").read_text(encoding="utf-8")

    assert "MODEL_PROVIDER_API_KEY=" not in env_template
    assert "AUTOPILOT_MODEL_PROVIDER_API_KEY_FILE=" in env_template
