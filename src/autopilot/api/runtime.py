"""Explicit production dependency assembly for the FastAPI control plane."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Self

from fastapi import FastAPI
from langgraph.checkpoint.postgres import PostgresSaver
from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from autopilot.api.app import ApiDependencies, create_app
from autopilot.api.repositories import (
    SqlAlchemyArtifactQuery,
    SqlAlchemyDeploymentStore,
    SqlAlchemyExperimentStore,
    SqlAlchemyPlanStore,
)
from autopilot.domain.base import NonEmptyStr
from autopilot.domain.enums import UserRole
from autopilot.domain.identifiers import UserId
from autopilot.domain.identities import BearerTokenBinding, BearerTokenHash, HumanSubject
from autopilot.gateway.authentication import BearerTokenAuthenticator
from autopilot.graph.model_provider import ModelProvider, OpenAICompatibleModelProvider
from autopilot.graph.reconciliation import ReconciliationPort
from autopilot.graph.runtime_defaults import UnavailableModelProvider, UnavailableReconciler
from autopilot.graph.workflow import GraphDependencies, UnavailableGraphOperations, build_runtime
from autopilot.infrastructure.artifacts import LocalArtifactStore
from autopilot.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)

MODEL_PROVIDER_CONFIGURATION_INVALID = "ModelProvider configuration must be complete"
SUBJECT_CONFIGURATION_INVALID = "operator and admin must use different UserId values"


class ApiSettings(BaseSettings):
    """Trusted deployment configuration; no field is writable through an Agent request."""

    model_config = SettingsConfigDict(
        env_prefix="AUTOPILOT_API_",
        extra="forbid",
        frozen=True,
    )

    database_url: str = Field(min_length=1)
    checkpoint_database_url: str = Field(min_length=1)
    artifact_root: Path
    operator_user_id: UserId
    operator_token_hash: BearerTokenHash
    admin_user_id: UserId
    admin_token_hash: BearerTokenHash
    model_provider_base_url: AnyHttpUrl | None = None
    model_provider_api_key: SecretStr | None = None
    model_provider_model: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_provider_and_subjects(self) -> Self:
        provider_fields = (
            self.model_provider_base_url,
            self.model_provider_api_key,
            self.model_provider_model,
        )
        if any(field is not None for field in provider_fields) and not all(
            field is not None for field in provider_fields
        ):
            raise ValueError(MODEL_PROVIDER_CONFIGURATION_INVALID)
        if self.operator_user_id == self.admin_user_id:
            raise ValueError(SUBJECT_CONFIGURATION_INVALID)
        return self

    def token_bindings(self) -> tuple[BearerTokenBinding, ...]:
        return (
            BearerTokenBinding(
                token_hash=self.operator_token_hash,
                subject=HumanSubject(user_id=self.operator_user_id, role=UserRole.OPERATOR),
            ),
            BearerTokenBinding(
                token_hash=self.admin_token_hash,
                subject=HumanSubject(user_id=self.admin_user_id, role=UserRole.ADMIN),
            ),
        )

    def model_provider(self) -> ModelProvider:
        if (
            self.model_provider_base_url is None
            or self.model_provider_api_key is None
            or self.model_provider_model is None
        ):
            return UnavailableModelProvider()
        return OpenAICompatibleModelProvider(
            base_url=str(self.model_provider_base_url),
            api_key=self.model_provider_api_key,
            model=self.model_provider_model,
        )


@contextmanager
def _open_checkpoint(database_url: str) -> Iterator[PostgresSaver]:
    """Use the official saver lifecycle and initialize its tables before serving traffic."""
    with PostgresSaver.from_conn_string(database_url) as saver:
        saver.setup()
        yield saver


def _reconciler() -> ReconciliationPort:
    return UnavailableReconciler()


def create_production_app(settings: ApiSettings | None = None) -> FastAPI:
    """Build the API with PostgreSQL-backed state and explicit shutdown cleanup."""
    resolved = settings or ApiSettings()
    engine = create_postgres_engine(resolved.database_url)
    sessions = create_session_factory(engine)
    artifact_store = LocalArtifactStore(resolved.artifact_root)
    checkpoint_context = _open_checkpoint(resolved.checkpoint_database_url)
    try:
        checkpoint = checkpoint_context.__enter__()
        dependencies = ApiDependencies(
            authenticator=BearerTokenAuthenticator(resolved.token_bindings()),
            experiments=SqlAlchemyExperimentStore(sessions),
            graph=build_runtime(
                GraphDependencies(
                    model_provider=resolved.model_provider(),
                    operations=UnavailableGraphOperations(),
                    reconciler=_reconciler(),
                ),
                checkpointer=checkpoint,
            ),
            plans=SqlAlchemyPlanStore(sessions),
            deployments=SqlAlchemyDeploymentStore(sessions),
            artifacts=SqlAlchemyArtifactQuery(sessions, artifact_store),
        )
    except BaseException:
        checkpoint_context.__exit__(None, None, None)
        engine.dispose()
        raise

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            checkpoint_context.__exit__(None, None, None)
            engine.dispose()

    return create_app(dependencies, lifespan=lifespan)


__all__ = ["ApiSettings", "create_production_app"]
