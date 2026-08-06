"""Deployment Provider ports implemented by the vLLM Runner adapter."""

from typing import Protocol

from autopilot.capabilities.deployment.domain.models import CompiledVllmDeployment
from autopilot.capabilities.deployment.ports.models import (
    DeploymentAdapterCapabilities,
    DeploymentOperation,
    DeploymentStartContext,
)


class DeploymentAdapter(Protocol):
    """Closed lifecycle surface for the one supported vLLM Provider."""

    def capabilities(self) -> DeploymentAdapterCapabilities: ...

    def validate(self, compiled: CompiledVllmDeployment) -> None: ...

    def start(
        self,
        compiled: CompiledVllmDeployment,
        context: DeploymentStartContext,
    ) -> DeploymentOperation: ...

    def status(self, context: DeploymentStartContext) -> DeploymentOperation: ...

    def cancel(self, context: DeploymentStartContext) -> DeploymentOperation: ...
