"""Validate M2 capability manifests against code and repository structure."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, Field, StringConstraints, ValidationError, model_validator

from autopilot.capabilities.benchmark.application.service import BenchmarkPreview
from autopilot.capabilities.benchmark.domain.models import (
    BenchmarkExecutionSpecification,
    BenchmarkResult,
    CompiledEvalScopeBenchmark,
    EvalScopeVersionProfile,
)
from autopilot.capabilities.benchmark.ports.models import EvalScopeRawReport
from autopilot.capabilities.deployment.domain.models import (
    CompiledVllmDeployment,
    DeploymentExecutionSpecification,
    DeploymentPreview,
    VllmVersionProfile,
)
from autopilot.capabilities.evidence.domain.models import (
    ChampionPolicy,
    EvidenceBundle,
    EvidenceBundleManifest,
)
from autopilot.capabilities.optimization.application.verification import VerificationRunState
from autopilot.capabilities.optimization.domain.models import VllmSearchSpaceSpec
from autopilot.domain.base import NonEmptyStr, StrictModel
from autopilot.domain.enums import ExperimentPhase, RiskLevel
from autopilot.domain.trials import ChampionSelection, VerificationSummary

CapabilityName = Literal["deployment", "benchmark", "optimization", "evidence"]
ProviderName = Literal["vllm", "evalscope", "optuna", "mlflow"]
ImplementationStatus = Literal["implemented", "planned"]
SemanticVersion = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", max_length=32),
]

API_VERSION: Final = "autopilot/v1"
PACKAGE_KIND: Final = "CapabilityPackage"
PACKAGE_VERSION: Final = "0.1.0"
REQUIRED_DIRECTORIES: Final = (
    "domain",
    "tools",
    "application",
    "ports",
    "adapters",
    "tests",
)
TOOL_PREFIXES: Final = ("create_", "preview_", "start_", "get_", "cancel_")
DUPLICATE_REQUIREMENTS_ERROR: Final = "capability requirements must not contain duplicate values"
INVALID_TOOL_NAME_ERROR: Final = "Agent Tool name must use an approved action prefix"
DUPLICATE_MODES_ERROR: Final = "supported modes must not contain duplicates"
DUPLICATE_TOOLS_ERROR: Final = "capability Tool names must be unique"
DUPLICATE_SCHEMAS_ERROR: Final = "capability Schema versions must be unique"
PLANNED_PROVIDER_ENTRYPOINT_ERROR: Final = "planned Provider execution cannot declare an entrypoint"
INCOMPLETE_PROVIDER_EXECUTION_ERROR: Final = (
    "implemented Provider execution requires pinned versions and entrypoint"
)


class CapabilityMetadata(StrictModel):
    name: CapabilityName
    package_version: SemanticVersion


class ProviderDeclaration(StrictModel):
    name: ProviderName
    version_constraint: NonEmptyStr | None
    adapter_version: NonEmptyStr | None
    execution_entrypoint: NonEmptyStr | None


class ImplementationDeclaration(StrictModel):
    deterministic_core: ImplementationStatus
    agent_tools: ImplementationStatus
    provider_execution: ImplementationStatus


class ResourceRequirements(StrictModel):
    cpu: bool
    network: bool
    gpu: bool


class CapabilityRequirements(StrictModel):
    phases: tuple[ExperimentPhase, ...] = Field(min_length=1)
    resources: ResourceRequirements
    environment_capabilities: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def validate_unique_values(self) -> Self:
        if len(self.phases) != len(set(self.phases)) or len(self.environment_capabilities) != len(
            set(self.environment_capabilities)
        ):
            raise ValueError(DUPLICATE_REQUIREMENTS_ERROR)
        return self


class ToolDeclaration(StrictModel):
    name: NonEmptyStr
    visibility: Literal["dynamic"]
    risk_level: RiskLevel

    @model_validator(mode="after")
    def validate_name(self) -> Self:
        if not self.name.startswith(TOOL_PREFIXES):
            raise ValueError(INVALID_TOOL_NAME_ERROR)
        return self


class SupportedModes(StrictModel):
    modes: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_modes(self) -> Self:
        if len(self.modes) != len(set(self.modes)):
            raise ValueError(DUPLICATE_MODES_ERROR)
        return self


class CapabilityManifest(StrictModel):
    api_version: Literal["autopilot/v1"]
    kind: Literal["CapabilityPackage"]
    metadata: CapabilityMetadata
    provider: ProviderDeclaration
    implementation: ImplementationDeclaration
    requires: CapabilityRequirements
    tools: tuple[ToolDeclaration, ...] = Field(min_length=1)
    schema_versions: tuple[NonEmptyStr, ...] = Field(min_length=1)
    supports: SupportedModes

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        tool_names = tuple(tool.name for tool in self.tools)
        if len(tool_names) != len(set(tool_names)):
            raise ValueError(DUPLICATE_TOOLS_ERROR)
        if len(self.schema_versions) != len(set(self.schema_versions)):
            raise ValueError(DUPLICATE_SCHEMAS_ERROR)
        if self.implementation.provider_execution == "planned":
            if self.provider.execution_entrypoint is not None:
                raise ValueError(PLANNED_PROVIDER_ENTRYPOINT_ERROR)
        elif any(
            value is None
            for value in (
                self.provider.version_constraint,
                self.provider.adapter_version,
                self.provider.execution_entrypoint,
            )
        ):
            raise ValueError(INCOMPLETE_PROVIDER_EXECUTION_ERROR)
        return self


@dataclass(frozen=True)
class ExpectedTool:
    name: str
    risk_level: RiskLevel


@dataclass(frozen=True)
class CapabilitySpec:
    name: CapabilityName
    provider: ProviderName
    phases: tuple[ExperimentPhase, ...]
    resources: ResourceRequirements
    environment_capabilities: tuple[str, ...]
    tools: tuple[ExpectedTool, ...]
    schema_models: tuple[type[BaseModel], ...]
    modes: tuple[str, ...]


class ManifestValidationError(ValueError):
    """A deterministic collection of capability manifest validation failures."""

    def __init__(self, errors: tuple[str, ...]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


M2_CAPABILITY_SPECS: Final = (
    CapabilitySpec(
        name="deployment",
        provider="vllm",
        phases=(ExperimentPhase.DEPLOYMENT,),
        resources=ResourceRequirements(cpu=True, network=True, gpu=True),
        environment_capabilities=(
            "single_nvidia_gpu",
            "docker_gpu",
            "vllm_single_gpu_candidate",
        ),
        tools=(
            ExpectedTool("create_deployment_plan", RiskLevel.L0),
            ExpectedTool("start_deployment", RiskLevel.L2),
            ExpectedTool("get_deployment_status", RiskLevel.L0),
            ExpectedTool("get_deployment_result", RiskLevel.L0),
            ExpectedTool("cancel_deployment", RiskLevel.L2),
        ),
        schema_models=(
            VllmVersionProfile,
            DeploymentExecutionSpecification,
            CompiledVllmDeployment,
            DeploymentPreview,
        ),
        modes=("single_gpu",),
    ),
    CapabilitySpec(
        name="benchmark",
        provider="evalscope",
        phases=(
            ExperimentPhase.BENCHMARK,
            ExperimentPhase.OPTIMIZATION,
            ExperimentPhase.VERIFICATION,
        ),
        resources=ResourceRequirements(cpu=True, network=True, gpu=False),
        environment_capabilities=("openai_compatible_endpoint",),
        tools=(
            ExpectedTool("create_benchmark_plan", RiskLevel.L0),
            ExpectedTool("start_benchmark", RiskLevel.L2),
            ExpectedTool("get_benchmark_status", RiskLevel.L0),
            ExpectedTool("get_benchmark_result", RiskLevel.L0),
            ExpectedTool("cancel_benchmark", RiskLevel.L2),
        ),
        schema_models=(
            BenchmarkExecutionSpecification,
            EvalScopeVersionProfile,
            CompiledEvalScopeBenchmark,
            BenchmarkPreview,
            EvalScopeRawReport,
            BenchmarkResult,
        ),
        modes=("baseline", "closed_loop_sweep", "open_loop_sweep", "sla_search"),
    ),
    CapabilitySpec(
        name="optimization",
        provider="optuna",
        phases=(ExperimentPhase.OPTIMIZATION, ExperimentPhase.VERIFICATION),
        resources=ResourceRequirements(cpu=True, network=False, gpu=False),
        environment_capabilities=(),
        tools=(
            ExpectedTool("create_optimization_plan", RiskLevel.L0),
            ExpectedTool("start_optimization", RiskLevel.L2),
            ExpectedTool("get_optimization_status", RiskLevel.L0),
            ExpectedTool("get_optimization_result", RiskLevel.L0),
            ExpectedTool("cancel_optimization", RiskLevel.L2),
        ),
        schema_models=(VllmSearchSpaceSpec, VerificationRunState),
        modes=("single_objective", "top_candidate_verification"),
    ),
    CapabilitySpec(
        name="evidence",
        provider="mlflow",
        phases=(
            ExperimentPhase.OPTIMIZATION,
            ExperimentPhase.VERIFICATION,
            ExperimentPhase.REPORT,
        ),
        resources=ResourceRequirements(cpu=True, network=True, gpu=False),
        environment_capabilities=(),
        tools=(
            ExpectedTool("get_trial_comparison_result", RiskLevel.L0),
            ExpectedTool("create_champion_plan", RiskLevel.L0),
            ExpectedTool("get_evidence_result", RiskLevel.L0),
        ),
        schema_models=(
            ChampionPolicy,
            VerificationSummary,
            ChampionSelection,
            EvidenceBundleManifest,
            EvidenceBundle,
        ),
        modes=("champion_selection", "evidence_bundle"),
    ),
)


def _model_schema_version(model: type[BaseModel]) -> str:
    field = model.model_fields.get("schema_version")
    if field is None or not isinstance(field.default, str):
        raise ManifestValidationError((f"{model.__name__} has no static schema_version",))
    return field.default


def _load_manifest(path: Path) -> CapabilityManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ManifestValidationError(
            (f"cannot read JSON-compatible YAML {path}: {error}",)
        ) from error
    try:
        return CapabilityManifest.model_validate(payload)
    except ValidationError as error:
        raise ManifestValidationError((f"invalid capability manifest {path}: {error}",)) from error


def _record_mismatch(
    errors: list[str],
    label: str,
    actual: object,
    expected: object,
) -> None:
    if actual != expected:
        errors.append(f"{label}: expected {expected!r}, got {actual!r}")


def validate_capability_package(
    package_directory: Path, spec: CapabilitySpec
) -> CapabilityManifest:
    """Validate one M2 capability package and return its typed manifest."""
    errors = [
        f"{spec.name}: missing required directory {directory_name}"
        for directory_name in REQUIRED_DIRECTORIES
        if not (package_directory / directory_name).is_dir()
    ]

    adapter_directory = package_directory / "adapters"
    provider_directory = adapter_directory / spec.provider
    if not provider_directory.is_dir():
        errors.append(f"{spec.name}: missing Provider adapter directory {spec.provider}")
    if adapter_directory.is_dir():
        adapter_names = tuple(
            sorted(
                path.name
                for path in adapter_directory.iterdir()
                if path.is_dir() and path.name != "__pycache__"
            )
        )
        _record_mismatch(
            errors,
            f"{spec.name} Provider adapter directories",
            adapter_names,
            (spec.provider,),
        )

    manifest = _load_manifest(package_directory / "manifest.yaml")
    _record_mismatch(errors, f"{spec.name} directory name", package_directory.name, spec.name)
    _record_mismatch(errors, f"{spec.name} metadata name", manifest.metadata.name, spec.name)
    _record_mismatch(
        errors,
        f"{spec.name} package version",
        manifest.metadata.package_version,
        PACKAGE_VERSION,
    )
    _record_mismatch(errors, f"{spec.name} Provider", manifest.provider.name, spec.provider)
    _record_mismatch(errors, f"{spec.name} phases", manifest.requires.phases, spec.phases)
    _record_mismatch(errors, f"{spec.name} resources", manifest.requires.resources, spec.resources)
    _record_mismatch(
        errors,
        f"{spec.name} environment capabilities",
        manifest.requires.environment_capabilities,
        spec.environment_capabilities,
    )
    actual_tools = tuple(ExpectedTool(tool.name, tool.risk_level) for tool in manifest.tools)
    _record_mismatch(errors, f"{spec.name} Tool contracts", actual_tools, spec.tools)
    expected_schema_versions = tuple(_model_schema_version(model) for model in spec.schema_models)
    _record_mismatch(
        errors,
        f"{spec.name} Schema versions",
        manifest.schema_versions,
        expected_schema_versions,
    )
    _record_mismatch(errors, f"{spec.name} supported modes", manifest.supports.modes, spec.modes)
    _record_mismatch(
        errors,
        f"{spec.name} deterministic core status",
        manifest.implementation.deterministic_core,
        "implemented",
    )
    _record_mismatch(
        errors,
        f"{spec.name} Agent Tool status",
        manifest.implementation.agent_tools,
        "planned",
    )
    _record_mismatch(
        errors,
        f"{spec.name} Provider execution status",
        manifest.implementation.provider_execution,
        "planned",
    )
    _record_mismatch(
        errors,
        f"{spec.name} Provider version constraint",
        manifest.provider.version_constraint,
        None,
    )
    _record_mismatch(
        errors,
        f"{spec.name} Adapter version",
        manifest.provider.adapter_version,
        None,
    )
    _record_mismatch(
        errors,
        f"{spec.name} Provider execution entrypoint",
        manifest.provider.execution_entrypoint,
        None,
    )
    if errors:
        raise ManifestValidationError(tuple(errors))
    return manifest


def validate_m2_capability_manifests(repository_root: Path) -> tuple[CapabilityManifest, ...]:
    """Validate every deterministic M2 capability manifest."""
    capabilities_root = repository_root / "src" / "autopilot" / "capabilities"
    manifests: list[CapabilityManifest] = []
    errors: list[str] = []
    for spec in M2_CAPABILITY_SPECS:
        try:
            manifests.append(validate_capability_package(capabilities_root / spec.name, spec))
        except ManifestValidationError as error:
            errors.extend(error.errors)
    if errors:
        raise ManifestValidationError(tuple(errors))
    return tuple(manifests)


def main() -> int:
    """Run the repository-level Manifest consistency check."""
    repository_root = Path(__file__).resolve().parents[1]
    try:
        manifests = validate_m2_capability_manifests(repository_root)
    except ManifestValidationError as error:
        sys.stderr.write(f"{error}\n")
        return 1
    sys.stdout.write(f"validated {len(manifests)} M2 capability manifests\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
