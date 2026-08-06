"""Pinned Optuna Study adapter using official ask/tell and Storage APIs."""

from __future__ import annotations

import math
from typing import NoReturn

import optuna
from optuna.exceptions import OptunaError
from optuna.samplers import TPESampler
from optuna.storages import BaseStorage, RDBStorage
from optuna.trial import FrozenTrial, TrialState
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

from autopilot.capabilities.optimization.domain.enums import (
    OptimizationProviderCode,
    OptimizationProviderTrialState,
)
from autopilot.capabilities.optimization.domain.errors import OptimizationProviderError
from autopilot.capabilities.optimization.domain.models import (
    CompiledOptunaStudy,
    OptimizationProviderTrial,
    OptimizationStudyRef,
    OptimizationSuggestion,
    OptimizationTrialOutcome,
    OptunaVersionProfile,
)
from autopilot.domain.enums import TrialStatus
from autopilot.domain.hashing import compute_content_hash

_STUDY_HASH_ATTR = "autopilot.study_material_hash"
_DOMAIN_STATUS_ATTR = "autopilot.domain_status"
_OBJECTIVE_ATTR = "autopilot.objective_value"
_PARAMETERS_HASH_ATTR = "autopilot.parameters_hash"
_CONFIGURATION_PARAMETER = "configuration_index"
_STORAGE_ERROR = "the Optuna Storage operation failed"


class OptunaStudyAdapter:
    """Persist Study/Trial sampling while leaving execution to the Controller."""

    def __init__(self, *, profile: OptunaVersionProfile, storage: BaseStorage) -> None:
        if optuna.__version__ != profile.provider_version:
            raise OptimizationProviderError(
                OptimizationProviderCode.PROFILE_UNVERIFIED,
                "the installed Optuna SDK does not match the registered profile",
                retryable=False,
            )
        self._profile = profile
        self._storage = storage

    @classmethod
    def from_postgres_url(
        cls,
        *,
        profile: OptunaVersionProfile,
        database_url: str,
    ) -> OptunaStudyAdapter:
        """Create the production RDB Storage with a fixed Optuna schema search path."""
        try:
            url = make_url(database_url)
        except ArgumentError as error:
            raise OptimizationProviderError(
                OptimizationProviderCode.STORAGE_FAILURE,
                _STORAGE_ERROR,
                retryable=False,
            ) from error
        if url.drivername != "postgresql+psycopg" or not url.database:
            raise OptimizationProviderError(
                OptimizationProviderCode.STORAGE_FAILURE,
                "Optuna Storage requires a postgresql+psycopg database URL",
                retryable=False,
            )
        try:
            storage = RDBStorage(
                url=database_url,
                engine_kwargs={
                    "connect_args": {"options": "-csearch_path=optuna"},
                    "pool_pre_ping": True,
                },
            )
        except (OptunaError, SQLAlchemyError) as error:
            raise OptimizationProviderError(
                OptimizationProviderCode.STORAGE_FAILURE,
                _STORAGE_ERROR,
                retryable=True,
            ) from error
        return cls(profile=profile, storage=storage)

    @property
    def profile(self) -> OptunaVersionProfile:
        return self._profile

    def create_or_load(self, study: CompiledOptunaStudy) -> OptimizationStudyRef:
        self._validate_compiled_profile(study)
        expected_hash = compute_content_hash(study)
        try:
            provider_study = optuna.create_study(
                storage=self._storage,
                sampler=self._sampler(study),
                study_name=study.provider_study_name,
                direction="maximize",
                load_if_exists=True,
            )
        except (OptunaError, SQLAlchemyError) as error:
            self._raise_storage_failure(error)
        existing_hash = provider_study.user_attrs.get(_STUDY_HASH_ATTR)
        if existing_hash is None:
            if provider_study.trials:
                raise OptimizationProviderError(
                    OptimizationProviderCode.STUDY_BINDING_CONFLICT,
                    "the existing Optuna Study has no immutable Autopilot binding",
                    retryable=False,
                )
            try:
                provider_study.set_user_attr(_STUDY_HASH_ATTR, expected_hash.root)
            except (OptunaError, SQLAlchemyError) as error:
                self._raise_storage_failure(error)
        elif existing_hash != expected_hash.root:
            raise OptimizationProviderError(
                OptimizationProviderCode.STUDY_BINDING_CONFLICT,
                "the Optuna Study is bound to different immutable Plan material",
                retryable=False,
            )
        return OptimizationStudyRef(
            provider_version=self._profile.provider_version,
            adapter_version=self._profile.adapter_version,
            provider_profile_version=self._profile.profile_version,
            study_id=study.study_id,
            provider_study_name=study.provider_study_name,
            study_material_hash=expected_hash,
        )

    def ask(
        self,
        study: CompiledOptunaStudy,
        reference: OptimizationStudyRef,
    ) -> OptimizationSuggestion:
        provider_study = self._load_bound_study(study, reference)
        try:
            trial = provider_study.ask()
            sampled = trial.suggest_categorical(
                _CONFIGURATION_PARAMETER,
                list(range(len(study.configurations))),
            )
        except (OptunaError, SQLAlchemyError) as error:
            self._raise_storage_failure(error)
        if type(sampled) is not int or not 0 <= sampled < len(study.configurations):
            raise OptimizationProviderError(
                OptimizationProviderCode.STORAGE_FAILURE,
                "the Optuna Trial returned an invalid configuration index",
                retryable=False,
            )
        parameters = study.configurations[sampled]
        try:
            trial.set_user_attr(_PARAMETERS_HASH_ATTR, compute_content_hash(parameters).root)
        except (OptunaError, SQLAlchemyError) as error:
            self._raise_storage_failure(error)
        return OptimizationSuggestion(
            study_id=study.study_id,
            trial_number=trial.number,
            configuration_index=sampled,
            parameters=parameters,
        )

    def tell(
        self,
        study: CompiledOptunaStudy,
        reference: OptimizationStudyRef,
        outcome: OptimizationTrialOutcome,
    ) -> OptimizationProviderTrial:
        provider_study = self._load_bound_study(study, reference)
        frozen = self._get_frozen_trial(provider_study, outcome.trial_number)
        if frozen.state.is_finished():
            self._validate_finished_outcome(frozen, outcome)
            return self._provider_trial(study, frozen)
        try:
            self._set_outcome_attributes(reference, outcome)
            if outcome.status is TrialStatus.COMPLETED:
                provider_study.tell(
                    outcome.trial_number,
                    outcome.objective_value,
                    state=TrialState.COMPLETE,
                )
            else:
                provider_study.tell(outcome.trial_number, state=TrialState.FAIL)
            frozen = self._get_frozen_trial(provider_study, outcome.trial_number)
        except OptimizationProviderError:
            raise
        except (KeyError, OptunaError, SQLAlchemyError) as error:
            self._raise_storage_failure(error)
        self._validate_finished_outcome(frozen, outcome)
        return self._provider_trial(study, frozen)

    def get_trials(
        self,
        study: CompiledOptunaStudy,
        reference: OptimizationStudyRef,
    ) -> tuple[OptimizationProviderTrial, ...]:
        provider_study = self._load_bound_study(study, reference)
        try:
            trials = provider_study.get_trials(deepcopy=True)
            return tuple(self._provider_trial(study, trial) for trial in trials)
        except OptimizationProviderError:
            raise
        except (KeyError, OptunaError, SQLAlchemyError) as error:
            self._raise_storage_failure(error)

    def _load_bound_study(
        self,
        study: CompiledOptunaStudy,
        reference: OptimizationStudyRef,
    ) -> optuna.study.Study:
        self._validate_reference(study, reference)
        try:
            provider_study = optuna.load_study(
                storage=self._storage,
                sampler=self._sampler(study),
                study_name=reference.provider_study_name,
            )
        except (KeyError, OptunaError, SQLAlchemyError) as error:
            self._raise_storage_failure(error)
        if provider_study.user_attrs.get(_STUDY_HASH_ATTR) != reference.study_material_hash.root:
            raise OptimizationProviderError(
                OptimizationProviderCode.STUDY_BINDING_CONFLICT,
                "the persisted Optuna Study binding changed",
                retryable=False,
            )
        return provider_study

    def _validate_reference(
        self,
        study: CompiledOptunaStudy,
        reference: OptimizationStudyRef,
    ) -> None:
        self._validate_compiled_profile(study)
        if (
            reference.provider_version != self._profile.provider_version
            or reference.adapter_version != self._profile.adapter_version
            or reference.provider_profile_version != self._profile.profile_version
        ):
            raise OptimizationProviderError(
                OptimizationProviderCode.PROFILE_UNVERIFIED,
                "the Optuna Study reference does not match the registered profile",
                retryable=False,
            )
        if (
            reference.study_id != study.study_id
            or reference.provider_study_name != study.provider_study_name
            or reference.study_material_hash != compute_content_hash(study)
        ):
            raise OptimizationProviderError(
                OptimizationProviderCode.STUDY_BINDING_CONFLICT,
                "the Optuna Study reference does not match compiled Plan material",
                retryable=False,
            )

    def _validate_compiled_profile(self, study: CompiledOptunaStudy) -> None:
        if (
            study.provider_version != self._profile.provider_version
            or study.adapter_version != self._profile.adapter_version
            or study.provider_profile_version != self._profile.profile_version
            or study.sampler != self._profile.sampler
        ):
            raise OptimizationProviderError(
                OptimizationProviderCode.PROFILE_UNVERIFIED,
                "the compiled Optuna Study does not match the registered profile",
                retryable=False,
            )

    def _provider_trial(
        self,
        study: CompiledOptunaStudy,
        frozen: FrozenTrial,
    ) -> OptimizationProviderTrial:
        raw_index = frozen.params.get(_CONFIGURATION_PARAMETER)
        if type(raw_index) is not int or not 0 <= raw_index < len(study.configurations):
            raise OptimizationProviderError(
                OptimizationProviderCode.STORAGE_FAILURE,
                "the persisted Optuna Trial has no valid compiled configuration",
                retryable=False,
            )
        parameters = study.configurations[raw_index]
        expected_parameters_hash = compute_content_hash(parameters).root
        if frozen.user_attrs.get(_PARAMETERS_HASH_ATTR) != expected_parameters_hash:
            raise OptimizationProviderError(
                OptimizationProviderCode.STUDY_BINDING_CONFLICT,
                "the persisted Optuna Trial parameters changed",
                retryable=False,
            )
        domain_status = self._domain_status(frozen)
        state = self._normalized_state(frozen.state)
        objective_value = (
            float(frozen.value)
            if state is OptimizationProviderTrialState.COMPLETED and frozen.value is not None
            else self._pending_objective(frozen, domain_status)
        )
        return OptimizationProviderTrial(
            suggestion=OptimizationSuggestion(
                study_id=study.study_id,
                trial_number=frozen.number,
                configuration_index=raw_index,
                parameters=parameters,
            ),
            state=state,
            objective_value=objective_value,
            domain_status=domain_status,
        )

    def _set_outcome_attributes(
        self,
        reference: OptimizationStudyRef,
        outcome: OptimizationTrialOutcome,
    ) -> None:
        try:
            study_id = self._storage.get_study_id_from_name(reference.provider_study_name)
            trial_id = self._storage.get_trial_id_from_study_id_trial_number(
                study_id,
                outcome.trial_number,
            )
            existing_status = self._storage.get_trial_user_attrs(trial_id).get(_DOMAIN_STATUS_ATTR)
        except (KeyError, OptunaError, SQLAlchemyError) as error:
            self._raise_storage_failure(error)
        if existing_status is not None and existing_status != outcome.status.value:
            raise OptimizationProviderError(
                OptimizationProviderCode.TRIAL_OUTCOME_CONFLICT,
                "the Optuna Trial is already bound to a different domain outcome",
                retryable=False,
            )
        try:
            self._storage.set_trial_user_attr(trial_id, _DOMAIN_STATUS_ATTR, outcome.status.value)
            if outcome.objective_value is not None:
                self._storage.set_trial_user_attr(
                    trial_id,
                    _OBJECTIVE_ATTR,
                    outcome.objective_value,
                )
        except (KeyError, OptunaError, SQLAlchemyError) as error:
            self._raise_storage_failure(error)

    @staticmethod
    def _get_frozen_trial(study: optuna.study.Study, trial_number: int) -> FrozenTrial:
        trial = next((item for item in study.get_trials() if item.number == trial_number), None)
        if trial is None:
            raise OptimizationProviderError(
                OptimizationProviderCode.TRIAL_NOT_FOUND,
                "the Optuna Trial does not exist in the bound Study",
                retryable=False,
            )
        return trial

    @staticmethod
    def _domain_status(frozen: FrozenTrial) -> TrialStatus | None:
        value = frozen.user_attrs.get(_DOMAIN_STATUS_ATTR)
        if value is None:
            return None
        try:
            return TrialStatus(value)
        except ValueError as error:
            raise OptimizationProviderError(
                OptimizationProviderCode.STUDY_BINDING_CONFLICT,
                "the persisted Optuna Trial has an invalid domain status",
                retryable=False,
            ) from error

    @staticmethod
    def _pending_objective(
        frozen: FrozenTrial,
        domain_status: TrialStatus | None,
    ) -> float | None:
        if frozen.state is not TrialState.RUNNING or domain_status is not TrialStatus.COMPLETED:
            return None
        value = frozen.user_attrs.get(_OBJECTIVE_ATTR)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise OptimizationProviderError(
                OptimizationProviderCode.STUDY_BINDING_CONFLICT,
                "the pending Optuna tell has an invalid objective value",
                retryable=False,
            )
        objective = float(value)
        if not math.isfinite(objective) or objective < 0:
            raise OptimizationProviderError(
                OptimizationProviderCode.STUDY_BINDING_CONFLICT,
                "the pending Optuna tell has an invalid objective value",
                retryable=False,
            )
        return objective

    @staticmethod
    def _normalized_state(state: TrialState) -> OptimizationProviderTrialState:
        if state in {TrialState.RUNNING, TrialState.WAITING}:
            return OptimizationProviderTrialState.RUNNING
        if state is TrialState.COMPLETE:
            return OptimizationProviderTrialState.COMPLETED
        return OptimizationProviderTrialState.FAILED

    @staticmethod
    def _validate_finished_outcome(
        frozen: FrozenTrial,
        outcome: OptimizationTrialOutcome,
    ) -> None:
        status = OptunaStudyAdapter._domain_status(frozen)
        completed_matches = (
            outcome.status is TrialStatus.COMPLETED
            and frozen.state is TrialState.COMPLETE
            and frozen.value is not None
            and outcome.objective_value is not None
            and math.isclose(float(frozen.value), outcome.objective_value, rel_tol=0, abs_tol=0)
        )
        failed_matches = (
            outcome.status is not TrialStatus.COMPLETED
            and frozen.state is TrialState.FAIL
            and frozen.value is None
        )
        if status is not outcome.status or not (completed_matches or failed_matches):
            raise OptimizationProviderError(
                OptimizationProviderCode.TRIAL_OUTCOME_CONFLICT,
                "the finished Optuna Trial does not match the domain outcome",
                retryable=False,
            )

    @staticmethod
    def _sampler(study: CompiledOptunaStudy) -> TPESampler:
        return TPESampler(seed=study.sampler_seed)

    @staticmethod
    def _raise_storage_failure(error: BaseException) -> NoReturn:
        raise OptimizationProviderError(
            OptimizationProviderCode.STORAGE_FAILURE,
            _STORAGE_ERROR,
            retryable=True,
        ) from error


__all__ = ["OptunaStudyAdapter"]
