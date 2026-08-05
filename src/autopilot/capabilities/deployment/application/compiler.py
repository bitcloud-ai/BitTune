"""Compile deployment plans into a closed vLLM provider DTO."""

from autopilot.capabilities.deployment.application.validator import (
    validate_deployment_specification,
)
from autopilot.capabilities.deployment.domain.models import (
    CompiledVllmDeployment,
    DeploymentExecutionSpecification,
    DeploymentHealthPolicy,
    DeploymentRuntimeLimits,
    VllmVersionProfile,
    compiled_arguments,
)


def compile_deployment(
    specification: DeploymentExecutionSpecification,
    profile: VllmVersionProfile,
) -> CompiledVllmDeployment:
    """Compile a validated immutable plan without performing I/O."""
    validate_deployment_specification(specification, profile)
    candidate = specification.candidate
    return CompiledVllmDeployment(
        provider_version=profile.provider_version,
        adapter_version=profile.adapter_version,
        provider_profile_version=profile.profile_version,
        candidate_id=candidate.candidate_id,
        engine_image=profile.engine_image,
        model_ref=candidate.model_ref,
        workload_hash=candidate.workload_hash,
        container_port=profile.container_port,
        arguments=compiled_arguments(candidate.parameters),
        runtime_limits=DeploymentRuntimeLimits(
            pid_limit=profile.pid_limit,
            startup_timeout_seconds=profile.startup_timeout_seconds,
            task_timeout_seconds=specification.budget.max_duration_seconds,
            health_check_timeout_seconds=profile.health_check_timeout_seconds,
            max_disk_growth_bytes=specification.budget.max_disk_growth_bytes,
        ),
        health_policy=DeploymentHealthPolicy(),
    )
