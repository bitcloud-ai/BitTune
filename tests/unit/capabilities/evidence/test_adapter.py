from __future__ import annotations

from collections.abc import Sequence

import pytest
from mlflow.entities import Experiment, Metric, Param, Run, RunData, RunInfo, RunTag
from mlflow.exceptions import MlflowException

from autopilot.capabilities.benchmark.domain.models import BenchmarkResult
from autopilot.capabilities.evidence.adapters.fake import FakeEvidenceAdapter
from autopilot.capabilities.evidence.adapters.mlflow import MlflowTrackingAdapter
from autopilot.capabilities.evidence.domain.enums import (
    EvidenceProviderState,
    EvidenceValidationCode,
)
from autopilot.capabilities.evidence.domain.errors import EvidenceProviderError
from autopilot.capabilities.evidence.domain.models import EvidenceRunRequest
from autopilot.domain.enums import ErrorCategory, TrialStatus
from autopilot.domain.errors import DomainError, ErrorEnvelope
from autopilot.domain.trials import TrialRecord

PROVIDER_FAILURE_TEXT = "Authorization: Bearer fake-secret"


class InMemoryMlflowClient:
    def __init__(self) -> None:
        self.experiments: dict[str, Experiment] = {}
        self.runs: dict[str, Run] = {}
        self.artifacts: dict[str, str] = {}
        self.create_run_calls = 0
        self.log_batch_calls = 0
        self.fail_log = False

    def get_experiment_by_name(self, name: str) -> Experiment | None:
        return next((item for item in self.experiments.values() if item.name == name), None)

    def create_experiment(
        self,
        name: str,
        artifact_location: str | None = None,
        tags: dict[str, object] | None = None,
    ) -> str:
        experiment_id = str(len(self.experiments) + 1)
        experiment = Experiment(
            experiment_id,
            name,
            artifact_location or f"file:///tmp/{experiment_id}",
            "active",
            tags=[RunTag(key, str(value)) for key, value in (tags or {}).items()],
        )
        self.experiments[experiment_id] = experiment
        return experiment_id

    def search_runs(
        self,
        experiment_ids: list[str],
        filter_string: str = "",
        *,
        max_results: int = 1_000,
    ) -> Sequence[Run]:
        marker = " = '"
        key = filter_string.split("`", maxsplit=2)[1]
        expected = filter_string.split(marker, maxsplit=1)[1].rstrip("'")
        matches = [
            run
            for run in self.runs.values()
            if run.info.experiment_id in experiment_ids and run.data.tags.get(key) == expected
        ]
        return matches[:max_results]

    def create_run(
        self,
        experiment_id: str,
        start_time: int | None = None,
        tags: dict[str, object] | None = None,
        run_name: str | None = None,
    ) -> Run:
        self.create_run_calls += 1
        run_id = f"run-{self.create_run_calls}"
        run = Run(
            RunInfo(
                run_id,
                experiment_id,
                "autopilot-worker",
                "RUNNING",
                start_time or 0,
                None,
                "active",
                run_name=run_name,
            ),
            RunData(tags=[RunTag(key, str(value)) for key, value in (tags or {}).items()]),
        )
        self.runs[run_id] = run
        return run

    def log_batch(
        self,
        run_id: str,
        metrics: Sequence[Metric] = (),
        params: Sequence[Param] = (),
        tags: Sequence[RunTag] = (),
        *,
        synchronous: bool | None = None,
    ) -> object | None:
        del synchronous
        if self.fail_log:
            raise MlflowException(PROVIDER_FAILURE_TEXT, error_code=2)
        self.log_batch_calls += 1
        run = self.runs[run_id]
        run.data.params.update({item.key: item.value for item in params})
        run.data.metrics.update({item.key: item.value for item in metrics})
        run.data.tags.update({item.key: item.value for item in tags})
        return None

    def log_text(self, run_id: str, text: str, artifact_file: str) -> None:
        self.artifacts[f"{run_id}/{artifact_file}"] = text

    def set_terminated(
        self,
        run_id: str,
        status: str | None = None,
        end_time: int | None = None,
    ) -> None:
        run = self.runs[run_id]
        info = run.info._copy_with_overrides(status=status, end_time=end_time)
        self.runs[run_id] = Run(info, run.data)

    def get_run(self, run_id: str) -> Run:
        return self.runs[run_id]


def test_mlflow_adapter_records_metrics_params_and_redacted_manifest(
    completed_request: EvidenceRunRequest,
    evidence_profile,
) -> None:
    client = InMemoryMlflowClient()
    adapter = MlflowTrackingAdapter(
        profile=evidence_profile,
        client=client,
        sdk_version="3.15.1",
    )

    reference = adapter.record_run(completed_request)
    run = client.runs[reference.provider_run_id]
    status = adapter.get_run_status(reference)

    assert status.state is EvidenceProviderState.SUCCEEDED
    assert run.info.status == "FINISHED"
    assert run.data.params["vllm.max_num_seqs"] == "8"
    assert run.data.metrics["throughput.successful_output_tokens_per_second"] == 100
    manifest = client.artifacts[f"{reference.provider_run_id}/evidence/run-manifest.json"]
    assert "Authorization" not in manifest
    assert "Bearer" not in manifest
    assert "raw_report_hash" in manifest


def test_mlflow_adapter_marks_oom_trial_constraints_as_failed(
    completed_request: EvidenceRunRequest,
    evidence_profile,
) -> None:
    benchmark_result = completed_request.benchmark_result
    assert benchmark_result is not None
    oom_result = BenchmarkResult.model_validate(
        {**benchmark_result.model_dump(mode="python"), "oom": True}
    )
    source_trial = completed_request.trial
    oom_trial = TrialRecord(
        trial_id=source_trial.trial_id,
        study_id=source_trial.study_id,
        trial_number=source_trial.trial_number,
        candidate_id=source_trial.candidate_id,
        parameters=source_trial.parameters,
        status=TrialStatus.OOM,
        evidence=source_trial.evidence,
        error=ErrorEnvelope(
            error=DomainError(
                code="TRIAL_BENCHMARK_OOM",
                category=ErrorCategory.OOM,
                message="normalized benchmark reported CUDA OOM",
                retryable=False,
                provider="evalscope",
            )
        ),
    )
    request = EvidenceRunRequest.model_validate(
        {
            **completed_request.model_dump(mode="python"),
            "trial": oom_trial,
            "benchmark_result": oom_result,
        }
    )
    client = InMemoryMlflowClient()
    adapter = MlflowTrackingAdapter(
        profile=evidence_profile,
        client=client,
        sdk_version="3.15.1",
    )

    reference = adapter.record_run(request)
    metrics = client.runs[reference.provider_run_id].data.metrics

    assert metrics["measurement.oom"] == 1
    assert metrics["constraints.satisfied"] == 0


def test_mlflow_adapter_replays_one_run_for_the_same_idempotency_key(
    failure_request: EvidenceRunRequest,
    evidence_profile,
) -> None:
    client = InMemoryMlflowClient()
    adapter = MlflowTrackingAdapter(
        profile=evidence_profile,
        client=client,
        sdk_version="3.15.1",
    )

    first = adapter.record_run(failure_request)
    replay = adapter.record_run(failure_request)

    assert replay.provider_run_id == first.provider_run_id
    assert replay.idempotent_replay is True
    assert client.create_run_calls == 1
    assert client.log_batch_calls == 1


def test_mlflow_adapter_rejects_idempotency_binding_conflict(
    failure_request: EvidenceRunRequest,
    evidence_profile,
) -> None:
    client = InMemoryMlflowClient()
    adapter = MlflowTrackingAdapter(
        profile=evidence_profile,
        client=client,
        sdk_version="3.15.1",
    )
    adapter.record_run(failure_request)
    changed = failure_request.model_copy(update={"code_revision": "c" * 40})

    with pytest.raises(EvidenceProviderError) as caught:
        adapter.record_run(changed)

    assert caught.value.code is EvidenceValidationCode.IDEMPOTENCY_CONFLICT
    assert caught.value.retryable is False


def test_mlflow_adapter_fails_closed_without_verified_profile(
    failure_request: EvidenceRunRequest,
) -> None:
    adapter = MlflowTrackingAdapter(profile=None, client=None, sdk_version=None)

    with pytest.raises(EvidenceProviderError) as caught:
        adapter.validate(failure_request)

    assert caught.value.code is EvidenceValidationCode.PROVIDER_UNAVAILABLE
    assert caught.value.retryable is False


def test_mlflow_provider_error_is_classified_without_secret_text(
    failure_request: EvidenceRunRequest,
    evidence_profile,
) -> None:
    client = InMemoryMlflowClient()
    client.fail_log = True
    adapter = MlflowTrackingAdapter(
        profile=evidence_profile,
        client=client,
        sdk_version="3.15.1",
    )

    with pytest.raises(EvidenceProviderError) as caught:
        adapter.record_run(failure_request)

    assert caught.value.code is EvidenceValidationCode.PROVIDER_UNAVAILABLE
    assert caught.value.retryable is True
    assert "fake-secret" not in str(caught.value)


def test_fake_evidence_adapter_has_the_same_idempotent_contract(
    failure_request: EvidenceRunRequest,
    evidence_profile,
) -> None:
    adapter = FakeEvidenceAdapter(evidence_profile)

    first = adapter.record_run(failure_request)
    replay = adapter.record_run(failure_request)
    status = adapter.get_run_status(first)

    assert replay.idempotent_replay is True
    assert status.state is EvidenceProviderState.FAILED
