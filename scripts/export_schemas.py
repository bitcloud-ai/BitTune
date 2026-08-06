"""Generate and verify public JSON Schema artifacts from Pydantic contracts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel

from autopilot.capabilities.benchmark.domain.models import (
    BenchmarkExecutionSpecification,
    BenchmarkResult,
)
from autopilot.capabilities.deployment.domain.models import DeploymentExecutionSpecification
from autopilot.capabilities.evidence.domain.models import (
    ChampionPolicy,
    EvidenceBundle,
    EvidenceBundleManifest,
)
from autopilot.capabilities.optimization.application.verification import VerificationRunState
from autopilot.capabilities.optimization.domain.models import VllmSearchSpaceSpec
from autopilot.domain.approvals import ApprovalRecord
from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.candidates import DeploymentCandidate
from autopilot.domain.constraints import SloSpec
from autopilot.domain.errors import ErrorEnvelope
from autopilot.domain.hardware import HardwarePassport
from autopilot.domain.identities import BearerTokenBinding
from autopilot.domain.jobs import JobRecord
from autopilot.domain.models import ModelProfile
from autopilot.domain.plans import PlanExecutionRequest
from autopilot.domain.requirements import RequirementSpec
from autopilot.domain.trials import ChampionSelection, TrialRecord, VerificationSummary
from autopilot.domain.workloads import WorkloadSpec
from autopilot.gateway.models import (
    JobAuthorizationRecord,
    ToolDefinition,
    ToolSetSnapshot,
)
from autopilot.policy.models import PolicyDecision, PolicyInput

SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "approval-v2": ApprovalRecord,
    "artifact-ref-v1": ArtifactRef,
    "bearer-token-binding-v1": BearerTokenBinding,
    "benchmark-execution-specification-v1": BenchmarkExecutionSpecification,
    "benchmark-result-v1": BenchmarkResult,
    "champion-policy-v1": ChampionPolicy,
    "champion-selection-v2": ChampionSelection,
    "deployment-candidate-v1": DeploymentCandidate,
    "deployment-execution-specification-v1": DeploymentExecutionSpecification,
    "error-envelope-v1": ErrorEnvelope,
    "evidence-bundle-manifest-v1": EvidenceBundleManifest,
    "evidence-bundle-v1": EvidenceBundle,
    "execution-budget-v1": ExecutionBudget,
    "hardware-passport-v1": HardwarePassport,
    "job-v1": JobRecord,
    "job-authorization-v1": JobAuthorizationRecord,
    "model-profile-v1": ModelProfile,
    "optimization-trial-v1": TrialRecord,
    "plan-execution-request-v1": PlanExecutionRequest,
    "policy-decision-v1": PolicyDecision,
    "policy-input-v1": PolicyInput,
    "requirements-v1": RequirementSpec,
    "slo-v1": SloSpec,
    "tool-definition-v1": ToolDefinition,
    "tool-set-snapshot-v1": ToolSetSnapshot,
    "verification-summary-v1": VerificationSummary,
    "verification-run-state-v1": VerificationRunState,
    "vllm-search-space-v1": VllmSearchSpaceSpec,
    "workload-v1": WorkloadSpec,
}


def schema_text(model: type[BaseModel]) -> str:
    """Render stable, reviewable JSON Schema text."""
    schema = model.model_json_schema(mode="validation")
    return f"{json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True)}\n"


def export_schemas(output_directory: Path) -> None:
    """Write every registered schema to its deterministic file."""
    output_directory.mkdir(parents=True, exist_ok=True)
    for name, model in sorted(SCHEMA_MODELS.items()):
        (output_directory / f"{name}.json").write_text(
            schema_text(model), encoding="utf-8", newline="\n"
        )


def verify_schemas(output_directory: Path) -> list[str]:
    """Return missing, stale, and unexpected generated schema files."""
    errors: list[str] = []
    expected_names = {f"{name}.json" for name in SCHEMA_MODELS}
    actual_names = {path.name for path in output_directory.glob("*.json")}
    errors.extend(
        f"missing generated schema: {missing}" for missing in sorted(expected_names - actual_names)
    )
    errors.extend(
        f"unexpected generated schema: {unexpected}"
        for unexpected in sorted(actual_names - expected_names)
    )
    for name, model in sorted(SCHEMA_MODELS.items()):
        path = output_directory / f"{name}.json"
        if path.is_file() and path.read_text(encoding="utf-8") != schema_text(model):
            errors.append(f"stale generated schema: {path.name}")
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="replace generated schema files")
    return parser.parse_args()


def main() -> int:
    """Write schemas when requested; otherwise verify checked-in artifacts."""
    args = parse_args()
    repository_root = Path(__file__).resolve().parents[1]
    output_directory = repository_root / "schemas"
    if args.write:
        export_schemas(output_directory)
        sys.stdout.write(f"generated {len(SCHEMA_MODELS)} JSON Schema files\n")
        return 0
    errors = verify_schemas(output_directory)
    if errors:
        for error in errors:
            sys.stderr.write(f"{error}\n")
        return 1
    sys.stdout.write(f"verified {len(SCHEMA_MODELS)} JSON Schema files\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
