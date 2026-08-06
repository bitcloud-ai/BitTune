from pathlib import Path

import pytest

from autopilot.api.runtime import ApiSettings
from autopilot.domain.enums import UserRole
from autopilot.domain.identifiers import ExperimentId, UserId
from autopilot.domain.identities import BearerTokenHash
from autopilot.graph.model_provider import ModelProviderError
from autopilot.graph.runtime_defaults import UnavailableModelProvider, UnavailableReconciler
from autopilot.graph.state import new_state


def _settings(tmp_path: Path) -> ApiSettings:
    return ApiSettings(
        database_url="postgresql+psycopg://autopilot:password@localhost/autopilot",
        checkpoint_database_url="postgresql://autopilot:password@localhost/autopilot",
        artifact_root=tmp_path,
        operator_user_id=UserId(root="user_" + "1" * 32),
        operator_token_hash=BearerTokenHash(root="sha256:" + "a" * 64),
        admin_user_id=UserId(root="user_" + "2" * 32),
        admin_token_hash=BearerTokenHash(root="sha256:" + "b" * 64),
        _secrets_dir=tmp_path,
    )


def test_settings_builds_separate_operator_and_admin_bindings(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    assert [binding.subject.role for binding in settings.token_bindings()] == [
        UserRole.OPERATOR,
        UserRole.ADMIN,
    ]
    assert isinstance(settings.model_provider(), UnavailableModelProvider)


def test_settings_rejects_partial_remote_provider_configuration(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="configuration must be complete"):
        ApiSettings(
            database_url="postgresql+psycopg://autopilot:password@localhost/autopilot",
            checkpoint_database_url="postgresql://autopilot:password@localhost/autopilot",
            artifact_root=tmp_path,
            operator_user_id=UserId(root="user_" + "1" * 32),
            operator_token_hash=BearerTokenHash(root="sha256:" + "a" * 64),
            admin_user_id=UserId(root="user_" + "2" * 32),
            admin_token_hash=BearerTokenHash(root="sha256:" + "b" * 64),
            model_provider_base_url="https://model.example.test/v1",
            _secrets_dir=tmp_path,
        )


def test_unavailable_defaults_fail_closed_for_provider_and_external_state() -> None:
    provider = UnavailableModelProvider()
    with pytest.raises(ModelProviderError):
        provider.parse_requirements("run")

    state = new_state(
        experiment_id=ExperimentId.new(),
        thread_id="thread",
        message="run",
    )
    state["active_job_id"] = "job_deadbeef"
    result = UnavailableReconciler().reconcile(state)
    assert result.requires_failure
