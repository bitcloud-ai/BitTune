"""Compile an immutable Optimization Study into the pinned Optuna DTO."""

from autopilot.capabilities.optimization.application.search_space import (
    enumerate_search_space,
)
from autopilot.capabilities.optimization.domain.models import (
    CompiledOptunaStudy,
    OptunaStudyDefinition,
    OptunaVersionProfile,
)
from autopilot.domain.identifiers import PlanHash


def compile_optuna_study(
    definition: OptunaStudyDefinition,
    plan_hash: PlanHash,
    profile: OptunaVersionProfile,
) -> CompiledOptunaStudy:
    """Compile only statically valid configurations in stable canonical order."""
    return CompiledOptunaStudy(
        provider_version=profile.provider_version,
        adapter_version=profile.adapter_version,
        provider_profile_version=profile.profile_version,
        study_id=definition.study_id,
        provider_study_name=str(definition.study_id),
        plan_hash=plan_hash,
        objective=definition.search_space.objective,
        sampler_seed=definition.sampler_seed,
        configurations=enumerate_search_space(
            definition.base_parameters,
            definition.search_space,
        ),
    )
