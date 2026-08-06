import pytest

from autopilot.capabilities.evidence.domain.models import EvidenceRunRef
from autopilot.capabilities.optimization.adapters.fake import FakeOptimizationTrialRepository
from autopilot.capabilities.optimization.domain.enums import TrialExecutionStage
from autopilot.capabilities.optimization.domain.errors import OptimizationTrialConflictError
from autopilot.capabilities.optimization.ports.models import (
    OptimizationTrialCheckpoint,
    OptimizationTrialCompletion,
    OptimizationTrialDraft,
    TrialBudgetReservation,
)
from autopilot.domain.candidates import VllmTuningSpec
from autopilot.domain.enums import TrialStatus
from autopilot.domain.identifiers import (
    BenchmarkRunId,
    CandidateId,
    ExperimentId,
    PlanHash,
    PlanId,
    StudyId,
    TrialId,
)
from autopilot.domain.trials import TrialRecord


def _draft() -> OptimizationTrialDraft:
    experiment_id = ExperimentId.new()
    return OptimizationTrialDraft(
        experiment_id=experiment_id,
        plan_id=PlanId.new(),
        plan_hash=PlanHash(root="sha256:" + "a" * 64),
        trial=TrialRecord(
            trial_id=TrialId.new(),
            study_id=StudyId.new(),
            trial_number=0,
            candidate_id=CandidateId.new(),
            parameters=VllmTuningSpec(
                max_model_len=8_192,
                gpu_memory_utilization=0.8,
                max_num_seqs=4,
                max_num_batched_tokens=8_192,
                enable_chunked_prefill=False,
            ),
            status=TrialStatus.SUGGESTED,
        ),
        benchmark_run_id=BenchmarkRunId.new(),
        reservation=TrialBudgetReservation(
            requests=10,
            duration_seconds=10,
            input_tokens=100,
            output_tokens=100,
        ),
    )


def _evidence(draft: OptimizationTrialDraft) -> EvidenceRunRef:
    return EvidenceRunRef(
        provider_version="3.15.1",
        adapter_version="0.1.0",
        provider_profile_version="mlflow-v1",
        provider_run_id="run-" + draft.trial.trial_id.root,
        experiment_id=draft.experiment_id,
        trial_id=draft.trial.trial_id,
        request_hash=PlanHash(root="sha256:" + "b" * 64),
    )


def test_fake_trial_repository_replays_pending_and_terminal_transitions() -> None:
    repository = FakeOptimizationTrialRepository()
    draft = _draft()
    first = repository.add_suggested(draft)
    assert repository.add_suggested(draft) == first

    checkpoint = OptimizationTrialCheckpoint(
        stage=TrialExecutionStage.BENCHMARK,
        provider_resource_id="evalscope-job-1",
    )
    pending = repository.mark_pending(draft.key(), checkpoint)
    assert pending.checkpoint == checkpoint
    assert repository.mark_pending(draft.key(), checkpoint) == pending

    completed = repository.complete(
        draft.key(),
        OptimizationTrialCompletion(
            trial=draft.trial.model_copy(update={"status": TrialStatus.CANCELLED}),
            evidence_run=_evidence(draft),
        ),
    )
    assert completed.trial.status is TrialStatus.CANCELLED
    assert completed.checkpoint is None
    assert repository.list_for_study(
        experiment_id=draft.experiment_id,
        study_id=draft.trial.study_id,
        plan_hash=draft.plan_hash,
    ) == (completed,)


def test_fake_trial_repository_rejects_terminal_rebinding() -> None:
    repository = FakeOptimizationTrialRepository()
    draft = _draft()
    repository.add_suggested(draft)
    completion = OptimizationTrialCompletion(
        trial=draft.trial.model_copy(update={"status": TrialStatus.CANCELLED}),
        evidence_run=_evidence(draft),
    )
    repository.complete(draft.key(), completion)

    with pytest.raises(OptimizationTrialConflictError):
        repository.complete(
            draft.key(),
            completion.model_copy(
                update={
                    "evidence_run": completion.evidence_run.model_copy(
                        update={"provider_run_id": "other"}
                    )
                }
            ),
        )
