"""Explicit production dependency assembly for the FastAPI control plane."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Self

import httpx
from fastapi import FastAPI
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.postgres import PostgresSaver
from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from autopilot.api.app import ApiDependencies, create_app
from autopilot.api.repositories import (
    SqlAlchemyApprovalStore,
    SqlAlchemyArtifactQuery,
    SqlAlchemyDeploymentStore,
    SqlAlchemyExperimentStore,
    SqlAlchemyPlanStore,
)
from autopilot.domain.base import NonEmptyStr, utc_now
from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.enums import UserRole
from autopilot.domain.identifiers import UserId
from autopilot.domain.identities import BearerTokenBinding, BearerTokenHash, HumanSubject
from autopilot.gateway.authentication import BearerTokenAuthenticator
from autopilot.gateway.models import GatewayEnvironment
from autopilot.gateway.mvp_tools import mvp_tool_registrations, provider_statuses
from autopilot.gateway.runtime import build_production_gateway
from autopilot.graph.agent import AgentGateway, AgentRuntime
from autopilot.graph.model_provider import ModelProvider, OpenAICompatibleModelProvider
from autopilot.graph.reconciliation import ReconciliationPort
from autopilot.graph.runtime_defaults import (
    UnavailableAgentGateway,
    UnavailableModelProvider,
    UnavailableReconciler,
)
from autopilot.graph.workflow import GraphDependencies, UnavailableGraphOperations, build_runtime
from autopilot.infrastructure.artifacts import LocalArtifactStore
from autopilot.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from autopilot.policy.opa import OpaPolicyClient

MODEL_PROVIDER_CONFIGURATION_INVALID = "ModelProvider configuration must be complete"
SUBJECT_CONFIGURATION_INVALID = "operator and admin must use different UserId values"


class ApiSettings(BaseSettings):
    """Trusted deployment configuration; no field is writable through an Agent request."""

    model_config = SettingsConfigDict(
        env_prefix="AUTOPILOT_API_",
        extra="forbid",
        frozen=True,
        secrets_dir="/run/secrets",
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
    opa_base_url: AnyHttpUrl | None = None
    agent_budget_ceiling: ExecutionBudget | None = None
    agent_verified_providers: tuple[NonEmptyStr, ...] = ()
    agent_hardware_capabilities: frozenset[str] = frozenset()
    agent_enabled_providers: frozenset[str] = frozenset()
    agent_enabled_feature_flags: frozenset[str] = frozenset()

    @model_validator(mode="before")
    @classmethod
    def normalize_empty_provider_fields(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        payload = dict(value)
        for name in (
            "model_provider_base_url",
            "model_provider_api_key",
            "model_provider_model",
        ):
            if payload.get(name) == "":
                payload[name] = None
        return payload

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

    def agent_model(self) -> BaseChatModel | None:
        if (
            self.model_provider_base_url is None
            or self.model_provider_api_key is None
            or self.model_provider_model is None
        ):
            return None
        return ChatOpenAI(
            base_url=str(self.model_provider_base_url),
            api_key=self.model_provider_api_key.get_secret_value(),
            model=self.model_provider_model,
            timeout=30.0,
            max_retries=0,
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
    opa_http_client: httpx.Client | None = None
    try:
        checkpoint = checkpoint_context.__enter__()
        agent_model = resolved.agent_model()
        registrations = mvp_tool_registrations()
        agent_gateway: AgentGateway = UnavailableAgentGateway()
        if resolved.opa_base_url is not None and resolved.agent_budget_ceiling is not None:
            opa_http_client = httpx.Client(base_url=str(resolved.opa_base_url), timeout=5.0)
            assembly = build_production_gateway(
                sessions=sessions,
                policy=OpaPolicyClient(opa_http_client),
                budget_ceiling=resolved.agent_budget_ceiling,
                provider_statuses=provider_statuses(verified=resolved.agent_verified_providers),
                clock=utc_now,
            )
            agent_gateway = assembly.gateway
            registrations = assembly.registrations
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
            agent=(
                AgentRuntime(
                    model=agent_model,
                    gateway=agent_gateway,
                    registrations=registrations,
                    checkpointer=checkpoint,
                )
                if agent_model is not None
                else None
            ),
            plans=SqlAlchemyPlanStore(sessions),
            approvals=SqlAlchemyApprovalStore(sessions),
            deployments=SqlAlchemyDeploymentStore(sessions),
            artifacts=SqlAlchemyArtifactQuery(sessions, artifact_store),
            agent_environment=(
                lambda experiment_id, subject: GatewayEnvironment(
                    experiment_id=experiment_id,
                    subject=subject,
                    hardware_capabilities=frozenset(resolved.agent_hardware_capabilities),
                    enabled_providers=frozenset(resolved.agent_enabled_providers),
                    enabled_feature_flags=frozenset(resolved.agent_enabled_feature_flags),
                )
            ),
        )
    except BaseException:
        if opa_http_client is not None:
            opa_http_client.close()
        checkpoint_context.__exit__(None, None, None)
        engine.dispose()
        raise

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if opa_http_client is not None:
                opa_http_client.close()
            checkpoint_context.__exit__(None, None, None)
            engine.dispose()

    return create_app(dependencies, lifespan=lifespan)


__all__ = ["ApiSettings", "create_production_app"]
