"""Strict Agent inputs owned by the environment capability."""

from typing import Literal

from autopilot.capabilities.environment.domain.models import EnvironmentInspectionSpecification
from autopilot.domain.base import StrictModel


class CreateEnvironmentPlanInput(StrictModel):
    schema_version: Literal["create-environment-plan-input/v1"] = "create-environment-plan-input/v1"
    specification: EnvironmentInspectionSpecification


__all__ = ["CreateEnvironmentPlanInput"]
