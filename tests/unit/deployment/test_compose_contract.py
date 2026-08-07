from pathlib import Path

COMPOSE = Path(__file__).parents[2] / ".." / "deploy" / "compose.yaml"
DOCKERFILE = COMPOSE.parent / "Dockerfile"
POSTGRES_INIT = COMPOSE.parent / "postgres" / "init-databases.sql"


def test_control_plane_compose_requires_immutable_images_and_api_hardening() -> None:
    document = COMPOSE.resolve().read_text(encoding="utf-8")

    assert ":latest" not in document
    assert "${AUTOPILOT_API_IMAGE:?" in document
    assert "${AUTOPILOT_POSTGRES_IMAGE:?" in document
    assert "${AUTOPILOT_OPA_IMAGE:?" in document
    assert "--set=decision_logs.console=true" in document

    mlflow = document.split("  mlflow:\n", maxsplit=1)[1].split("  migrate:\n", maxsplit=1)[0]
    assert "image: ${AUTOPILOT_API_IMAGE:?" in mlflow
    assert 'user: "65532:65532"' in mlflow
    assert "postgresql+psycopg://" in mlflow
    assert "@postgres:5432/autopilot_mlflow" in mlflow
    assert 'OPENBLAS_NUM_THREADS: "1"' in mlflow
    assert 'MLFLOW_SERVER_ENABLE_JOB_EXECUTION: "false"' in mlflow
    assert '--workers\n      - "1"' in mlflow
    assert "mlflow,mlflow:*,localhost,localhost:*,127.0.0.1,127.0.0.1:*" in mlflow
    assert "--artifacts-destination" in mlflow
    assert "--default-artifact-root" not in mlflow
    assert "http://127.0.0.1:5000/health" in mlflow
    assert "./postgres/init-databases.sql:/docker-entrypoint-initdb.d/" in document
    assert "CREATE DATABASE autopilot_mlflow;" in POSTGRES_INIT.read_text(encoding="utf-8")

    api = document.split("  autopilot-api:\n", maxsplit=1)[1]
    assert 'user: "65532:65532"' in api
    assert "read_only: true" in api
    assert 'cap_drop: ["ALL"]' in api
    assert "no-new-privileges:true" in api
    assert "docker.sock" not in api
    assert "gpu" not in api.casefold()
    assert "AUTOPILOT_API_OPA_BASE_URL: http://opa:8181" in api
    assert "AUTOPILOT_API_AGENT_BUDGET_CEILING:" in api
    assert "healthcheck:" in api
    assert "http://127.0.0.1:8000/healthz" in api


def test_deployment_template_does_not_embed_provider_secret_values() -> None:
    env_template = (COMPOSE.parent / ".env.example").read_text(encoding="utf-8")

    assert "MODEL_PROVIDER_API_KEY=" not in env_template
    assert "AUTOPILOT_MODEL_PROVIDER_API_KEY_FILE=" in env_template
    assert "target: AUTOPILOT_API_MODEL_PROVIDER_API_KEY" in COMPOSE.read_text(encoding="utf-8")


def test_control_plane_dockerfile_is_reproducible_and_non_root() -> None:
    document = DOCKERFILE.read_text(encoding="utf-8")

    assert document.startswith("# syntax=docker/dockerfile:1.7@sha256:")
    assert "python:3.12.13-slim@sha256:" in document
    assert "ghcr.io/astral-sh/uv:0.12.0@sha256:" in document
    assert "uv sync --locked --no-dev" in document
    assert "--extra evidence --extra optimization --extra tracking-server" in document
    assert "USER 65532:65532" in document
    assert "useradd --uid 65532 --gid 65532" in document
    assert "/artifacts /mlartifacts" in document
    assert "COPY ." not in document
    assert "latest" not in document
