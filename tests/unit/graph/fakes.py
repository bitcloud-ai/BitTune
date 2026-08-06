from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.constraints import BooleanConstraint, SloSpec
from autopilot.domain.enums import ErrorCategory
from autopilot.domain.errors import ErrorEnvelope
from autopilot.domain.identifiers import ModelRevision, UserId
from autopilot.domain.models import HuggingFaceModelRef
from autopilot.domain.requirements import RequirementSpec
from autopilot.domain.workloads import (
    SamplingSpec,
    SyntheticFixedDataset,
    TokenizerRef,
    WorkloadSpec,
)
from autopilot.graph.model_provider import (
    BenchmarkIntent,
    FailureAnalysis,
    ReportDraft,
)
from autopilot.graph.state import AutopilotState
from autopilot.graph.workflow import GraphOperationResult


def requirement_spec() -> RequirementSpec:
    revision = ModelRevision(root="1" * 40)
    return RequirementSpec(
        created_by=UserId(root="user_" + "2" * 32),
        model_ref=HuggingFaceModelRef(
            repository_id="Qwen/Qwen3-8B",
            revision=revision,
        ),
        priority="throughput",
        workload=WorkloadSpec(
            dataset=SyntheticFixedDataset(dataset_id="medium-v1"),
            tokenizer=TokenizerRef(repository_id="Qwen/Qwen3-8B", revision=revision),
            prompt_tokens=2_048,
            output_tokens=512,
            stream=True,
            ignore_eos=True,
            sampling=SamplingSpec(seed=20_260_805),
        ),
        slo=SloSpec(constraints=(BooleanConstraint(),)),
        budget=ExecutionBudget(
            max_duration_seconds=600,
            max_requests=5_000,
            max_input_tokens=10_000_000,
            max_output_tokens=1_000_000,
            max_disk_growth_bytes=20_000_000_000,
        ),
        allow_model_download=True,
        allow_container_start=True,
    )


class FakeModelProvider:
    def parse_requirements(self, message: str) -> RequirementSpec:
        assert message
        return requirement_spec()

    def propose_test_strategy(self, requirements: RequirementSpec) -> BenchmarkIntent:
        assert requirements.priority == "throughput"
        return BenchmarkIntent(profiles=("baseline", "closed_loop_sweep"), rationale="fixture")

    def analyze_failure(self, error: ErrorEnvelope) -> FailureAnalysis:
        assert error.error.category is ErrorCategory.INFRASTRUCTURE_ERROR
        return FailureAnalysis(summary="fixture", actions=("contact operator",))

    def write_report(self, evidence_refs: tuple[str, ...]) -> ReportDraft:
        assert evidence_refs
        return ReportDraft(markdown="# Evidence report")


class FakeGraphOperations:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def inspect_environment(self, _state: AutopilotState) -> GraphOperationResult:
        self.calls.append("environment")
        return GraphOperationResult(
            hardware_passport_ref="artifact://environment/passport",
            artifact_refs=("artifact://environment/passport",),
        )

    def estimate_capacity(self, _state: AutopilotState) -> GraphOperationResult:
        self.calls.append("capacity")
        return GraphOperationResult(
            candidate_refs=("cand_" + "3" * 32,),
            active_candidate_id="cand_" + "3" * 32,
            artifact_refs=("artifact://capacity/plan",),
        )

    def deploy_and_smoke_test(self, _state: AutopilotState) -> GraphOperationResult:
        self.calls.append("deployment")
        return GraphOperationResult(
            active_deployment_id="deployment_" + "4" * 32,
            artifact_refs=("artifact://deployment/smoke",),
        )

    def benchmark_baseline(self, _state: AutopilotState) -> GraphOperationResult:
        self.calls.append("baseline")
        return GraphOperationResult(
            benchmark_summary_refs=("artifact://benchmark/baseline",),
            artifact_refs=("artifact://benchmark/baseline",),
        )

    def benchmark_strategy(
        self, _state: AutopilotState, intent: BenchmarkIntent
    ) -> GraphOperationResult:
        self.calls.append("strategy")
        assert intent.profiles
        return GraphOperationResult(
            benchmark_summary_refs=("artifact://benchmark/sweep",),
            artifact_refs=("artifact://benchmark/sweep",),
        )

    def optimize(self, _state: AutopilotState) -> GraphOperationResult:
        self.calls.append("optimization")
        return GraphOperationResult(
            active_study_id="study_" + "5" * 32,
            trial_refs=tuple(f"trial_{index:032x}" for index in range(10)),
            artifact_refs=("artifact://optimization/study",),
        )

    def verify_top_candidates(self, _state: AutopilotState) -> GraphOperationResult:
        self.calls.append("verification")
        return GraphOperationResult(
            champion_ref="cand_" + "3" * 32,
            artifact_refs=("artifact://verification/summary",),
        )

    def archive_evidence(self, _state: AutopilotState, report: ReportDraft) -> GraphOperationResult:
        self.calls.append("evidence")
        assert report.markdown
        return GraphOperationResult(artifact_refs=("artifact://evidence/bundle",))
