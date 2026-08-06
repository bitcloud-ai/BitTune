from pathlib import Path

import optuna
import pytest
from optuna.storages import RDBStorage

from autopilot.capabilities.optimization.adapters.optuna import OptunaStudyAdapter
from autopilot.capabilities.optimization.application.compiler import compile_optuna_study
from autopilot.capabilities.optimization.domain.enums import (
    OptimizationProviderCode,
    OptimizationProviderTrialState,
)
from autopilot.capabilities.optimization.domain.errors import OptimizationProviderError
from autopilot.capabilities.optimization.domain.models import (
    GpuMemoryUtilizationRange,
    OptimizationTrialOutcome,
    OptunaStudyDefinition,
    OptunaVersionProfile,
    VllmSearchSpaceSpec,
)
from autopilot.domain.candidates import VllmTuningSpec
from autopilot.domain.constraints import BooleanConstraint, ObjectiveSpec, SloSpec
from autopilot.domain.enums import TrialStatus
from autopilot.domain.identifiers import PlanHash, StudyId


def _profile(*, provider_version: str | None = None) -> OptunaVersionProfile:
    return OptunaVersionProfile(
        profile_version="optuna-4.9.0-mvp-v1",
        provider_version=provider_version or optuna.__version__,
        adapter_version="0.1.0",
        contract_verified=True,
    )


def _definition(*, study_id: StudyId | None = None) -> OptunaStudyDefinition:
    return OptunaStudyDefinition(
        study_id=study_id or StudyId.new(),
        base_parameters=VllmTuningSpec(
            max_model_len=8_192,
            gpu_memory_utilization=0.8,
            max_num_seqs=4,
            max_num_batched_tokens=8_192,
            enable_chunked_prefill=False,
        ),
        search_space=VllmSearchSpaceSpec(
            profile_name="adapter-contract-v1",
            objective=ObjectiveSpec(),
            slo=SloSpec(constraints=(BooleanConstraint(),)),
            gpu_memory_utilization=GpuMemoryUtilizationRange(
                low=0.8,
                high=0.82,
                step=0.02,
            ),
            max_num_seqs=(4, 8),
            max_num_batched_tokens=(4_096, 8_192),
            enable_chunked_prefill=(False, True),
        ),
        sampler_seed=20260805,
    )


def _storage(path: Path) -> RDBStorage:
    return RDBStorage(url=f"sqlite:///{path.as_posix()}")


def _plan_hash(character: str = "a") -> PlanHash:
    return PlanHash(root=f"sha256:{character * 64}")


def test_optuna_adapter_persists_ask_tell_and_resumes_study(tmp_path: Path) -> None:
    profile = _profile()
    definition = _definition()
    compiled = compile_optuna_study(definition, _plan_hash(), profile)
    database_path = tmp_path / "optuna-contract.sqlite3"
    adapter = OptunaStudyAdapter(profile=profile, storage=_storage(database_path))

    reference = adapter.create_or_load(compiled)
    suggestion = adapter.ask(compiled, reference)
    assert suggestion.parameters in compiled.configurations
    completed = adapter.tell(
        compiled,
        reference,
        OptimizationTrialOutcome(
            trial_number=suggestion.trial_number,
            status=TrialStatus.COMPLETED,
            objective_value=123.5,
        ),
    )

    assert completed.state is OptimizationProviderTrialState.COMPLETED
    assert completed.domain_status is TrialStatus.COMPLETED
    assert completed.objective_value == 123.5

    resumed = OptunaStudyAdapter(profile=profile, storage=_storage(database_path))
    resumed_reference = resumed.create_or_load(compiled)
    assert resumed_reference == reference
    assert resumed.get_trials(compiled, resumed_reference) == (completed,)
    assert (
        resumed.tell(
            compiled,
            resumed_reference,
            OptimizationTrialOutcome(
                trial_number=suggestion.trial_number,
                status=TrialStatus.COMPLETED,
                objective_value=123.5,
            ),
        )
        == completed
    )


def test_optuna_adapter_records_classified_domain_failure(tmp_path: Path) -> None:
    profile = _profile()
    compiled = compile_optuna_study(_definition(), _plan_hash(), profile)
    adapter = OptunaStudyAdapter(
        profile=profile,
        storage=_storage(tmp_path / "failure.sqlite3"),
    )
    reference = adapter.create_or_load(compiled)
    suggestion = adapter.ask(compiled, reference)

    failed = adapter.tell(
        compiled,
        reference,
        OptimizationTrialOutcome(
            trial_number=suggestion.trial_number,
            status=TrialStatus.OOM,
        ),
    )

    assert failed.state is OptimizationProviderTrialState.FAILED
    assert failed.domain_status is TrialStatus.OOM
    assert failed.objective_value is None


def test_optuna_adapter_rejects_study_or_outcome_rebinding(tmp_path: Path) -> None:
    profile = _profile()
    definition = _definition()
    original = compile_optuna_study(definition, _plan_hash(), profile)
    rebound = compile_optuna_study(definition, _plan_hash("b"), profile)
    adapter = OptunaStudyAdapter(
        profile=profile,
        storage=_storage(tmp_path / "binding.sqlite3"),
    )
    reference = adapter.create_or_load(original)
    suggestion = adapter.ask(original, reference)
    adapter.tell(
        original,
        reference,
        OptimizationTrialOutcome(
            trial_number=suggestion.trial_number,
            status=TrialStatus.BENCHMARK_FAILED,
        ),
    )

    with pytest.raises(OptimizationProviderError) as study_error:
        adapter.create_or_load(rebound)
    assert study_error.value.code is OptimizationProviderCode.STUDY_BINDING_CONFLICT

    with pytest.raises(OptimizationProviderError) as outcome_error:
        adapter.tell(
            original,
            reference,
            OptimizationTrialOutcome(
                trial_number=suggestion.trial_number,
                status=TrialStatus.DEPLOYMENT_FAILED,
            ),
        )
    assert outcome_error.value.code is OptimizationProviderCode.TRIAL_OUTCOME_CONFLICT


def test_optuna_adapter_fails_closed_for_unpinned_sdk_or_database() -> None:
    with pytest.raises(OptimizationProviderError) as version_error:
        OptunaStudyAdapter(
            profile=_profile(provider_version="4.8.0"), storage=optuna.storages.InMemoryStorage()
        )
    assert version_error.value.code is OptimizationProviderCode.PROFILE_UNVERIFIED

    secret_url = "sqlite:///Authorization-Bearer-secret.sqlite3"  # noqa: S105
    with pytest.raises(OptimizationProviderError) as database_error:
        OptunaStudyAdapter.from_postgres_url(
            profile=_profile(),
            database_url=secret_url,
        )
    assert database_error.value.code is OptimizationProviderCode.STORAGE_FAILURE
    assert "Authorization" not in str(database_error.value)


def test_optuna_compiler_statically_removes_invalid_scheduler_combinations() -> None:
    profile = _profile()
    compiled = compile_optuna_study(_definition(), _plan_hash(), profile)

    assert len(compiled.configurations) == 12
    assert all(
        configuration.enable_chunked_prefill
        or configuration.max_num_batched_tokens >= configuration.max_model_len
        for configuration in compiled.configurations
    )
