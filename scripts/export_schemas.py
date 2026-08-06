"""Generate and verify public JSON Schema artifacts from Pydantic contracts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from autopilot.api.app import (
    AgentSessionView,
    ApiDependencies,
    ArtifactDownloadMeta,
    CreateExperimentRequest,
    CreateSessionRequest,
    ExperimentMessageRequest,
    ExperimentView,
    GraphRunView,
    JobView,
    PlanDecisionRequest,
    ResumeRequest,
    SessionMessageRequest,
    SessionResumeRequest,
    create_app,
)
from autopilot.api.repositories import DeploymentProjection, InMemoryExperimentStore, PlanProjection
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
from autopilot.gateway.authentication import BearerTokenAuthenticator
from autopilot.gateway.models import (
    JobAuthorizationRecord,
    ToolDefinition,
    ToolSetSnapshot,
)
from autopilot.graph.agent import AgentMessageView, AgentToolCallView
from autopilot.graph.model_provider import BenchmarkIntent, FailureAnalysis, ReportDraft
from autopilot.graph.runtime_defaults import UnavailableModelProvider, UnavailableReconciler
from autopilot.graph.state import GraphStateSnapshot
from autopilot.graph.workflow import GraphDependencies, UnavailableGraphOperations, build_runtime
from autopilot.policy.models import PolicyDecision, PolicyInput

SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "agent-message-view-v1": AgentMessageView,
    "agent-tool-call-view-v1": AgentToolCallView,
    "api-agent-session-view-v1": AgentSessionView,
    "api-artifact-download-meta-v1": ArtifactDownloadMeta,
    "api-create-experiment-request-v1": CreateExperimentRequest,
    "api-create-session-request-v1": CreateSessionRequest,
    "api-deployment-projection-v1": DeploymentProjection,
    "api-experiment-message-request-v1": ExperimentMessageRequest,
    "api-experiment-view-v1": ExperimentView,
    "api-graph-run-view-v1": GraphRunView,
    "api-job-view-v1": JobView,
    "api-plan-decision-request-v1": PlanDecisionRequest,
    "api-plan-projection-v1": PlanProjection,
    "api-resume-request-v1": ResumeRequest,
    "api-session-message-request-v1": SessionMessageRequest,
    "api-session-resume-request-v1": SessionResumeRequest,
    "approval-v2": ApprovalRecord,
    "artifact-ref-v1": ArtifactRef,
    "bearer-token-binding-v1": BearerTokenBinding,
    "benchmark-execution-specification-v1": BenchmarkExecutionSpecification,
    "benchmark-result-v1": BenchmarkResult,
    "benchmark-intent-v1": BenchmarkIntent,
    "champion-policy-v1": ChampionPolicy,
    "champion-selection-v2": ChampionSelection,
    "deployment-candidate-v1": DeploymentCandidate,
    "deployment-execution-specification-v1": DeploymentExecutionSpecification,
    "error-envelope-v1": ErrorEnvelope,
    "evidence-bundle-manifest-v1": EvidenceBundleManifest,
    "evidence-bundle-v1": EvidenceBundle,
    "execution-budget-v1": ExecutionBudget,
    "failure-analysis-v1": FailureAnalysis,
    "graph-state-v1": GraphStateSnapshot,
    "hardware-passport-v1": HardwarePassport,
    "job-v1": JobRecord,
    "job-authorization-v1": JobAuthorizationRecord,
    "model-profile-v1": ModelProfile,
    "optimization-trial-v1": TrialRecord,
    "plan-execution-request-v1": PlanExecutionRequest,
    "policy-decision-v1": PolicyDecision,
    "policy-input-v1": PolicyInput,
    "report-draft-v1": ReportDraft,
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


def openapi_text() -> str:
    """Render the FastAPI contract without opening a database or provider connection."""
    dependencies = ApiDependencies(
        authenticator=BearerTokenAuthenticator(()),
        experiments=InMemoryExperimentStore(),
        graph=build_runtime(
            GraphDependencies(
                model_provider=UnavailableModelProvider(),
                operations=UnavailableGraphOperations(),
                reconciler=UnavailableReconciler(),
            ),
            checkpointer=InMemorySaver(),
        ),
    )
    document = create_app(dependencies).openapi()
    return f"{json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)}\n"


def export_schemas(output_directory: Path) -> None:
    """Write every registered schema to its deterministic file."""
    output_directory.mkdir(parents=True, exist_ok=True)
    for name, model in sorted(SCHEMA_MODELS.items()):
        (output_directory / f"{name}.json").write_text(
            schema_text(model), encoding="utf-8", newline="\n"
        )
    (output_directory / "openapi.json").write_text(openapi_text(), encoding="utf-8", newline="\n")


def verify_schemas(output_directory: Path) -> list[str]:
    """Return missing, stale, and unexpected generated schema files."""
    errors: list[str] = []
    expected_names = {f"{name}.json" for name in SCHEMA_MODELS} | {"openapi.json"}
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
    openapi_path = output_directory / "openapi.json"
    if openapi_path.is_file() and openapi_path.read_text(encoding="utf-8") != openapi_text():
        errors.append("stale generated schema: openapi.json")
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
