"""Environment inspection orchestration over typed collectors."""

from autopilot.capabilities.environment.application.normalizer import build_hardware_passport
from autopilot.capabilities.environment.application.validator import (
    validate_environment_specification,
    validate_rtx_5090_snapshot,
)
from autopilot.capabilities.environment.domain.models import (
    EnvironmentInspectionResult,
    EnvironmentInspectionSpecification,
    EnvironmentVersionProfile,
)
from autopilot.capabilities.environment.ports import (
    EnvironmentArtifactSink,
    LinuxHostCollector,
    NvmlCollector,
)
from autopilot.domain.identifiers import HardwarePassportId


class EnvironmentInspectionService:
    """Collect, validate, record, and normalize one Hardware Passport."""

    def __init__(
        self,
        *,
        profile: EnvironmentVersionProfile,
        nvml: NvmlCollector,
        host: LinuxHostCollector,
        artifacts: EnvironmentArtifactSink,
    ) -> None:
        self._profile = profile
        self._nvml = nvml
        self._host = host
        self._artifacts = artifacts

    @property
    def profile(self) -> EnvironmentVersionProfile:
        return self._profile

    def validate(self, specification: EnvironmentInspectionSpecification) -> None:
        validate_environment_specification(specification, self._profile)

    def inspect(
        self,
        specification: EnvironmentInspectionSpecification,
    ) -> EnvironmentInspectionResult:
        self.validate(specification)
        gpu = self._nvml.collect_gpu_zero()
        snapshot = self._host.collect(gpu=gpu, driver_version=self._nvml.driver_version)
        validate_rtx_5090_snapshot(snapshot, self._profile)
        raw_artifact = self._artifacts.write_environment_artifact(
            snapshot,
            adapter_version=self._profile.adapter_version,
        )
        passport_id = HardwarePassportId.new()
        passport = build_hardware_passport(
            snapshot,
            self._profile,
            raw_artifact,
            hardware_passport_id=passport_id,
        )
        return EnvironmentInspectionResult(
            hardware_passport=passport,
            hardware_passport_id=passport_id,
        )
