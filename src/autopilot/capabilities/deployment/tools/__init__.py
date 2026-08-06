"""Strict Agent inputs owned by the deployment capability."""

from typing import Literal

from autopilot.capabilities.deployment.domain.models import DeploymentExecutionSpecification
from autopilot.domain.base import StrictModel


class CreateDeploymentPlanInput(StrictModel):
    schema_version: Literal["create-deployment-plan-input/v1"] = "create-deployment-plan-input/v1"
    specification: DeploymentExecutionSpecification


__all__ = ["CreateDeploymentPlanInput"]
