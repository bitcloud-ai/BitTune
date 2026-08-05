from pathlib import Path

from autopilot.capabilities.benchmark.domain.models import BenchmarkResult
from autopilot.capabilities.optimization.application.evaluator import (
    evaluate_slo,
    objective_value,
    select_top_trial_candidates,
)
from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.candidates import VllmTuningSpec
from autopilot.domain.constraints import NumericConstraint, SloSpec
from autopilot.domain.enums import NumericMetric, NumericOperator, TrialStatus
from autopilot.domain.identifiers import CandidateId, StudyId, TrialId
from autopilot.domain.provenance import MeasuredProvenance
from autopilot.domain.trials import ConstraintEvaluation, NumericMetricValue, TrialRecord


def parameters() -> VllmTuningSpec:
    return VllmTuningSpec(
        max_model_len=8_192,
        gpu_memory_utilization=0.9,
        max_num_seqs=8,
        max_num_batched_tokens=4_096,
        enable_chunked_prefill=True,
    )


NORMALIZED_RESULT_GOLDEN = (
    Path(__file__).parents[4]
    / "src"
    / "autopilot"
    / "capabilities"
    / "benchmark"
    / "tests"
    / "golden"
    / "normalized-result.expected.json"
)


def test_evaluator_uses_normalized_metrics() -> None:
    result = BenchmarkResult.model_validate_json(
        NORMALIZED_RESULT_GOLDEN.read_text(encoding="utf-8")
    )
    slo = SloSpec(
        constraints=(
            NumericConstraint(
                metric=NumericMetric.TTFT_P95_MS,
                operator=NumericOperator.LESS_THAN_OR_EQUAL,
                value=250,
            ),
            NumericConstraint(
                metric=NumericMetric.SUCCESS_RATE,
                operator=NumericOperator.GREATER_THAN_OR_EQUAL,
                value=0.95,
            ),
        )
    )
    evaluations = evaluate_slo(result, slo)

    assert tuple(evaluation.passed for evaluation in evaluations) == (True, False)
    assert objective_value(result).value == result.throughput.successful_output_tokens_per_second


def trial(
    objective: float,
    status: TrialStatus,
    candidate_id: CandidateId,
    artifact: ArtifactRef,
    provenance: MeasuredProvenance,
) -> TrialRecord:
    constraint = NumericConstraint(
        metric=NumericMetric.SUCCESS_RATE,
        operator=NumericOperator.GREATER_THAN_OR_EQUAL,
        value=0.9,
    )
    passed = status is TrialStatus.COMPLETED
    evaluation = ConstraintEvaluation(
        constraint=constraint,
        observed=NumericMetricValue(metric=NumericMetric.SUCCESS_RATE, value=1 if passed else 0),
        passed=passed,
    )
    return TrialRecord(
        trial_id=TrialId.new(),
        study_id=StudyId(root=f"study_{'1' * 32}"),
        trial_number=int(objective),
        candidate_id=candidate_id,
        parameters=parameters(),
        status=status,
        objective=NumericMetricValue(
            metric=NumericMetric.SUCCESSFUL_OUTPUT_TOKENS_PER_SECOND,
            value=objective,
        ),
        constraints=(evaluation,),
        provenance=provenance,
        evidence=(artifact,),
    )


def test_top_candidates_exclude_constraint_failures(
    capability_artifact_ref: ArtifactRef,
    capability_measured_provenance: MeasuredProvenance,
) -> None:
    candidates = tuple(CandidateId.new() for _ in range(4))
    trials = (
        trial(
            100,
            TrialStatus.COMPLETED,
            candidates[0],
            capability_artifact_ref,
            capability_measured_provenance,
        ),
        trial(
            120,
            TrialStatus.CONSTRAINT_FAILED,
            candidates[1],
            capability_artifact_ref,
            capability_measured_provenance,
        ),
        trial(
            90,
            TrialStatus.COMPLETED,
            candidates[2],
            capability_artifact_ref,
            capability_measured_provenance,
        ),
        trial(
            80,
            TrialStatus.COMPLETED,
            candidates[3],
            capability_artifact_ref,
            capability_measured_provenance,
        ),
    )

    selected = select_top_trial_candidates(trials)

    assert selected == (candidates[0], candidates[2], candidates[3])
