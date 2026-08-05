"""Read-only deployment preview service."""

from autopilot.capabilities.deployment.application.compiler import compile_deployment
from autopilot.capabilities.deployment.domain.models import (
    DeploymentExecutionSpecification,
    DeploymentPreview,
    VllmVersionProfile,
)


def preview_deployment(
    specification: DeploymentExecutionSpecification,
    profile: VllmVersionProfile,
) -> DeploymentPreview:
    """Return the deterministic impact preview for an immutable deployment plan."""
    return DeploymentPreview(compiled=compile_deployment(specification, profile))
