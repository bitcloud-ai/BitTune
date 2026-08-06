"""Pinned MLflow Tracking adapter for automatic Trial evidence recording."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import NoReturn, Protocol, cast

import mlflow
from mlflow import MlflowClient
from mlflow.entities import Experiment, Metric, Param, Run, RunTag
from mlflow.exceptions import MlflowException

from autopilot.capabilities.evidence.domain.enums import (
    EvidenceProviderState,
    EvidenceValidationCode,
)
from autopilot.capabilities.evidence.domain.errors import EvidenceProviderError
from autopilot.capabilities.evidence.domain.models import (
    EvidenceRunRef,
    EvidenceRunRequest,
    EvidenceRunStatus,
    EvidenceVersionProfile,
)
from autopilot.domain.artifacts import ArtifactRef
from autopilot.domain.enums import TrialStatus
from autopilot.domain.hashing import compute_content_hash
from autopilot.domain.identifiers import Sha256Digest

_IDEMPOTENCY_TAG = "autopilot.idempotency_key"
_REQUEST_HASH_TAG = "autopilot.request_hash"
_EXPERIMENT_TAG = "autopilot.experiment_id"
_TRIAL_TAG = "autopilot.trial_id"
_CANDIDATE_TAG = "autopilot.candidate_id"
_TRIAL_STATUS_TAG = "autopilot.trial_status"
_PROFILE_TAG = "autopilot.evidence_profile"
_SCHEMA_TAG = "autopilot.schema_version"
_MANIFEST_PATH = "evidence/run-manifest.json"

_RETRYABLE_MLFLOW_CODES = frozenset(
    {
        "DEADLINE_EXCEEDED",
        "INTERNAL_ERROR",
        "REQUEST_LIMIT_EXCEEDED",
        "TEMPORARILY_UNAVAILABLE",
    }
)
_TERMINAL_MLFLOW_STATES = frozenset({"FINISHED", "FAILED", "KILLED"})


class MlflowTrackingClient(Protocol):
    """The stable ``MlflowClient`` surface used by this adapter."""

    def get_experiment_by_name(self, name: str) -> Experiment | None: ...

    def create_experiment(
        self,
        name: str,
        artifact_location: str | None = None,
        tags: dict[str, object] | None = None,
    ) -> str: ...

    def search_runs(
        self,
        experiment_ids: list[str],
        filter_string: str = "",
        *,
        max_results: int = 1_000,
    ) -> Sequence[Run]: ...

    def create_run(
        self,
        experiment_id: str,
        start_time: int | None = None,
        tags: dict[str, object] | None = None,
        run_name: str | None = None,
    ) -> Run: ...

    def log_batch(
        self,
        run_id: str,
        metrics: Sequence[Metric] = (),
        params: Sequence[Param] = (),
        tags: Sequence[RunTag] = (),
        *,
        synchronous: bool | None = None,
    ) -> object | None: ...

    def log_text(self, run_id: str, text: str, artifact_file: str) -> None: ...

    def set_terminated(
        self,
        run_id: str,
        status: str | None = None,
        end_time: int | None = None,
    ) -> None: ...

    def get_run(self, run_id: str) -> Run: ...


class MlflowTrackingAdapter:
    """Record only validated Autopilot evidence through the official MLflow SDK."""

    def __init__(
        self,
        *,
        profile: EvidenceVersionProfile | None,
        client: MlflowTrackingClient | None,
        sdk_version: str | None,
    ) -> None:
        self._profile = profile
        self._client = client
        self._sdk_version = sdk_version

    @classmethod
    def from_tracking_uri(
        cls,
        *,
        profile: EvidenceVersionProfile | None,
        tracking_uri: str,
    ) -> MlflowTrackingAdapter:
        """Create the official SDK client at the configuration boundary."""
        return cls(
            profile=profile,
            client=cast(MlflowTrackingClient, MlflowClient(tracking_uri=tracking_uri)),
            sdk_version=mlflow.__version__,
        )

    @property
    def profile(self) -> EvidenceVersionProfile:
        profile = self._profile
        if profile is None:
            raise EvidenceProviderError(
                EvidenceValidationCode.PROVIDER_UNAVAILABLE,
                "the verified MLflow profile is not configured",
                retryable=False,
            )
        return profile

    def validate(self, request: EvidenceRunRequest) -> None:
        profile = self._profile
        if profile is None or self._client is None or self._sdk_version is None:
            raise EvidenceProviderError(
                EvidenceValidationCode.PROVIDER_UNAVAILABLE,
                "the verified MLflow profile and Tracking client are not configured",
                retryable=False,
            )
        if self._sdk_version != profile.provider_version:
            raise EvidenceProviderError(
                EvidenceValidationCode.PROFILE_UNVERIFIED,
                "the installed MLflow SDK does not match the registered profile",
                retryable=False,
            )
        if request.trial.status is TrialStatus.SUGGESTED:
            raise EvidenceProviderError(
                EvidenceValidationCode.PROVIDER_REJECTED,
                "non-terminal Trials cannot be recorded as evidence",
                retryable=False,
            )

    def record_run(self, request: EvidenceRunRequest) -> EvidenceRunRef:
        self.validate(request)
        request_hash = compute_content_hash(request)
        experiment_id = self._get_or_create_experiment(request)
        existing = self._find_idempotent_run(experiment_id, request)
        replay = existing is not None
        run = existing or self._create_run(experiment_id, request, request_hash)
        reference = self._reference(run, request, request_hash, replay=replay)
        if replay and run.info.status in _TERMINAL_MLFLOW_STATES:
            if run.info.status != _terminal_mlflow_status(request.trial.status):
                raise EvidenceProviderError(
                    EvidenceValidationCode.INVALID_PROVIDER_STATE,
                    "the existing MLflow Run terminal state conflicts with the Trial",
                    retryable=False,
                )
            return reference
        self._log_run(run.info.run_id, request, request_hash)
        return reference

    def get_run_status(self, run: EvidenceRunRef) -> EvidenceRunStatus:
        profile = self.profile
        if (
            run.provider_version != profile.provider_version
            or run.adapter_version != profile.adapter_version
            or run.provider_profile_version != profile.profile_version
        ):
            raise EvidenceProviderError(
                EvidenceValidationCode.PROFILE_UNVERIFIED,
                "the MLflow Run reference does not match the registered profile",
                retryable=False,
            )
        client = self._require_client()
        try:
            provider_run = client.get_run(run.provider_run_id)
        except MlflowException as error:
            code = (
                EvidenceValidationCode.RUN_NOT_FOUND
                if error.error_code == "RESOURCE_DOES_NOT_EXIST"
                else EvidenceValidationCode.PROVIDER_REJECTED
            )
            self._raise_mlflow_error("read Run status", error, code=code)
        self._validate_existing_run(provider_run, run.experiment_id.root, run.trial_id.root, run)
        return EvidenceRunStatus(
            run=run,
            state=_provider_state(provider_run.info.status),
            started_at=_timestamp(provider_run.info.start_time),
            ended_at=_timestamp(provider_run.info.end_time),
        )

    def _get_or_create_experiment(self, request: EvidenceRunRequest) -> str:
        client = self._require_client()
        name = f"autopilot-{request.experiment_id}"
        try:
            experiment = client.get_experiment_by_name(name)
            if experiment is not None:
                return str(experiment.experiment_id)
            return str(
                client.create_experiment(
                    name,
                    tags={_EXPERIMENT_TAG: str(request.experiment_id)},
                )
            )
        except MlflowException as error:
            if error.error_code == "RESOURCE_ALREADY_EXISTS":
                try:
                    existing = client.get_experiment_by_name(name)
                except MlflowException as lookup_error:
                    self._raise_mlflow_error("resolve Experiment", lookup_error)
                if existing is not None:
                    return str(existing.experiment_id)
            self._raise_mlflow_error("create Experiment", error)

    def _find_idempotent_run(
        self,
        experiment_id: str,
        request: EvidenceRunRequest,
    ) -> Run | None:
        client = self._require_client()
        filter_string = f"tags.`{_IDEMPOTENCY_TAG}` = '{request.idempotency_key.root}'"
        try:
            matches = client.search_runs(
                [experiment_id],
                filter_string=filter_string,
                max_results=2,
            )
        except MlflowException as error:
            self._raise_mlflow_error("search idempotent Run", error)
        if len(matches) > 1:
            raise EvidenceProviderError(
                EvidenceValidationCode.IDEMPOTENCY_CONFLICT,
                "multiple MLflow Runs share one Autopilot idempotency key",
                retryable=False,
            )
        if not matches:
            return None
        run = matches[0]
        expected_hash = compute_content_hash(request)
        self._validate_existing_run(
            run,
            request.experiment_id.root,
            request.trial.trial_id.root,
            expected_hash,
        )
        return run

    def _create_run(
        self,
        experiment_id: str,
        request: EvidenceRunRequest,
        request_hash: Sha256Digest,
    ) -> Run:
        tags: dict[str, object] = {
            _SCHEMA_TAG: request.schema_version,
            _PROFILE_TAG: self.profile.profile_version,
            _IDEMPOTENCY_TAG: request.idempotency_key.root,
            _REQUEST_HASH_TAG: request_hash.root,
            _EXPERIMENT_TAG: str(request.experiment_id),
            _TRIAL_TAG: str(request.trial.trial_id),
            _CANDIDATE_TAG: str(request.candidate.candidate_id),
            _TRIAL_STATUS_TAG: request.trial.status.value,
        }
        try:
            return self._require_client().create_run(
                experiment_id,
                start_time=_milliseconds(request.started_at),
                tags=tags,
                run_name=f"trial-{request.trial.trial_number}",
            )
        except MlflowException as error:
            self._raise_mlflow_error("create Run", error)

    def _log_run(
        self,
        run_id: str,
        request: EvidenceRunRequest,
        request_hash: Sha256Digest,
    ) -> None:
        client = self._require_client()
        try:
            client.log_batch(
                run_id,
                metrics=_metrics(request),
                params=_parameters(request),
                tags=(),
                synchronous=True,
            )
            client.log_text(
                run_id,
                _manifest_json(request, request_hash),
                _MANIFEST_PATH,
            )
            client.set_terminated(
                run_id,
                status=_terminal_mlflow_status(request.trial.status),
                end_time=_milliseconds(request.ended_at),
            )
        except MlflowException as error:
            self._raise_mlflow_error("record Run evidence", error)

    def _reference(
        self,
        run: Run,
        request: EvidenceRunRequest,
        request_hash: Sha256Digest,
        *,
        replay: bool,
    ) -> EvidenceRunRef:
        profile = self.profile
        return EvidenceRunRef(
            provider_version=profile.provider_version,
            adapter_version=profile.adapter_version,
            provider_profile_version=profile.profile_version,
            provider_run_id=run.info.run_id,
            experiment_id=request.experiment_id,
            trial_id=request.trial.trial_id,
            request_hash=request_hash,
            idempotent_replay=replay,
        )

    @staticmethod
    def _validate_existing_run(
        run: Run,
        experiment_id: str,
        trial_id: str,
        expected: EvidenceRunRef | Sha256Digest,
    ) -> None:
        if isinstance(expected, EvidenceRunRef):
            expected_hash = expected.request_hash.root
        else:
            expected_hash = expected.root
        tags = run.data.tags
        if (
            tags.get(_REQUEST_HASH_TAG) != expected_hash
            or tags.get(_EXPERIMENT_TAG) != experiment_id
            or tags.get(_TRIAL_TAG) != trial_id
        ):
            raise EvidenceProviderError(
                EvidenceValidationCode.IDEMPOTENCY_CONFLICT,
                "the MLflow Run is bound to different immutable evidence",
                retryable=False,
            )

    def _require_client(self) -> MlflowTrackingClient:
        client = self._client
        if client is None:
            raise EvidenceProviderError(
                EvidenceValidationCode.PROVIDER_UNAVAILABLE,
                "the MLflow Tracking client is not configured",
                retryable=False,
            )
        return client

    @staticmethod
    def _raise_mlflow_error(
        operation: str,
        error: MlflowException,
        *,
        code: EvidenceValidationCode = EvidenceValidationCode.PROVIDER_REJECTED,
    ) -> NoReturn:
        retryable = error.error_code in _RETRYABLE_MLFLOW_CODES
        if retryable:
            code = EvidenceValidationCode.PROVIDER_UNAVAILABLE
        raise EvidenceProviderError(
            code,
            f"MLflow could not {operation} ({error.error_code})",
            retryable=retryable,
        ) from error


def _terminal_mlflow_status(status: TrialStatus) -> str:
    if status in {TrialStatus.COMPLETED, TrialStatus.CONSTRAINT_FAILED}:
        return "FINISHED"
    if status is TrialStatus.CANCELLED:
        return "KILLED"
    if status is TrialStatus.SUGGESTED:
        raise EvidenceProviderError(
            EvidenceValidationCode.PROVIDER_REJECTED,
            "non-terminal Trials cannot terminate an MLflow Run",
            retryable=False,
        )
    return "FAILED"


def _provider_state(status: str) -> EvidenceProviderState:
    states = {
        "SCHEDULED": EvidenceProviderState.SCHEDULED,
        "RUNNING": EvidenceProviderState.RUNNING,
        "FINISHED": EvidenceProviderState.SUCCEEDED,
        "FAILED": EvidenceProviderState.FAILED,
        "KILLED": EvidenceProviderState.CANCELLED,
    }
    try:
        return states[status]
    except KeyError as error:
        raise EvidenceProviderError(
            EvidenceValidationCode.INVALID_PROVIDER_STATE,
            "MLflow returned a state outside the registered profile",
            retryable=False,
        ) from error


def _parameters(request: EvidenceRunRequest) -> tuple[Param, ...]:
    candidate = request.candidate
    parameters = candidate.parameters
    model_ref = candidate.model_ref
    values = [
        ("trial.number", str(request.trial.trial_number)),
        ("trial.study_id", str(request.trial.study_id)),
        ("trial.status", request.trial.status.value),
        ("hardware.passport_id", str(candidate.hardware_passport_id)),
        ("hardware.passport_hash", candidate.hardware_passport_hash.root),
        ("model.type", model_ref.type),
        ("model.revision", model_ref.revision.root),
        ("engine.name", candidate.engine),
        ("engine.version", candidate.engine_version),
        ("engine.image_digest", candidate.engine_image.root),
        ("engine.adapter_version", candidate.adapter_version),
        ("workload.hash", candidate.workload_hash.root),
        ("code.revision", request.code_revision),
        ("vllm.tensor_parallel_size", str(parameters.tensor_parallel_size)),
        ("vllm.max_model_len", str(parameters.max_model_len)),
        ("vllm.gpu_memory_utilization", str(parameters.gpu_memory_utilization)),
        ("vllm.max_num_seqs", str(parameters.max_num_seqs)),
        ("vllm.max_num_batched_tokens", str(parameters.max_num_batched_tokens)),
        ("vllm.enable_chunked_prefill", str(parameters.enable_chunked_prefill).lower()),
        ("vllm.trust_remote_code", str(parameters.trust_remote_code).lower()),
    ]
    if model_ref.type == "huggingface":
        values.append(("model.repository_id", model_ref.repository_id))
    else:
        values.extend(
            (
                ("model.artifact_id", str(model_ref.artifact.artifact_id)),
                ("model.artifact_hash", model_ref.artifact.sha256.root),
            )
        )
    result = request.benchmark_result
    if result is not None:
        values.extend(
            (
                ("benchmark.provider", result.provider),
                ("benchmark.provider_version", result.provider_version),
                ("benchmark.adapter_version", result.adapter_version),
                ("benchmark.profile_version", result.provider_profile_version),
                ("benchmark.profile_hash", result.provider_profile_hash.root),
                ("benchmark.compiled_hash", result.compiled_benchmark_hash.root),
                ("benchmark.raw_report_hash", result.raw_report_hash.root),
                ("benchmark.traffic_mode", result.traffic_mode.value),
            )
        )
    error = request.trial.error
    if error is not None:
        values.extend(
            (
                ("failure.code", error.error.code),
                ("failure.category", error.error.category.value),
                ("failure.retryable", str(error.error.retryable).lower()),
            )
        )
    return tuple(Param(key, value) for key, value in values)  # type: ignore[no-untyped-call]


def _metrics(request: EvidenceRunRequest) -> tuple[Metric, ...]:
    result = request.benchmark_result
    if result is None:
        return ()
    timestamp = _milliseconds(request.ended_at)
    step = request.trial.trial_number
    values = (
        ("latency.e2e_p50_ms", result.latency.e2e_ms.p50),
        ("latency.e2e_p95_ms", result.latency.e2e_ms.p95),
        ("latency.e2e_p99_ms", result.latency.e2e_ms.p99),
        ("latency.ttft_p50_ms", result.latency.ttft_ms.p50),
        ("latency.ttft_p95_ms", result.latency.ttft_ms.p95),
        ("latency.ttft_p99_ms", result.latency.ttft_ms.p99),
        ("latency.tpot_p50_ms", result.latency.tpot_ms.p50),
        ("latency.tpot_p95_ms", result.latency.tpot_ms.p95),
        ("latency.tpot_p99_ms", result.latency.tpot_ms.p99),
        ("latency.itl_p50_ms", result.latency.itl_ms.p50),
        ("latency.itl_p95_ms", result.latency.itl_ms.p95),
        ("latency.itl_p99_ms", result.latency.itl_ms.p99),
        ("throughput.requests_per_second", result.throughput.requests_per_second),
        (
            "throughput.successful_requests_per_minute",
            result.throughput.successful_requests_per_minute,
        ),
        ("throughput.input_tokens_per_second", result.throughput.input_tokens_per_second),
        (
            "throughput.successful_output_tokens_per_second",
            result.throughput.successful_output_tokens_per_second,
        ),
        ("throughput.total_tokens_per_minute", result.throughput.total_tokens_per_minute),
        ("reliability.submitted", float(result.reliability.submitted)),
        ("reliability.completed", float(result.reliability.completed)),
        ("reliability.failed", float(result.reliability.failed)),
        ("reliability.timed_out", float(result.reliability.timed_out)),
        (
            "reliability.completed_within_window",
            float(result.reliability.completed_within_window),
        ),
        ("reliability.success_rate", result.reliability.success_rate),
        ("reliability.window_completion_ratio", result.reliability.window_completion_ratio),
        ("measurement.scheduled_window_seconds", result.scheduled_window_seconds),
        ("measurement.duration_seconds", result.measurement_duration_seconds),
        ("measurement.oom", float(result.oom)),
        (
            "constraints.satisfied",
            float(all(evaluation.passed for evaluation in request.trial.constraints)),
        ),
    )
    return tuple(Metric(key, value, timestamp, step) for key, value in values)


def _manifest_json(request: EvidenceRunRequest, request_hash: Sha256Digest) -> str:
    trial_payload = request.trial.model_dump(mode="json", exclude={"error"})
    failure = request.trial.error
    failure_payload: dict[str, object] | None = None
    if failure is not None:
        failure_payload = {
            "code": failure.error.code,
            "category": failure.error.category.value,
            "retryable": failure.error.retryable,
            "provider": failure.error.provider,
        }
    payload: dict[str, object] = {
        "schema_version": "evidence-run-manifest/v1",
        "request_hash": request_hash.root,
        "experiment_id": str(request.experiment_id),
        "candidate": request.candidate.model_dump(mode="json"),
        "trial": trial_payload,
        "benchmark_result": (
            request.benchmark_result.model_dump(mode="json")
            if request.benchmark_result is not None
            else None
        ),
        "failure": failure_payload,
        "artifacts": [artifact.model_dump(mode="json") for artifact in _artifacts(request)],
        "code_revision": request.code_revision,
        "started_at": request.started_at.isoformat(),
        "ended_at": request.ended_at.isoformat(),
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _artifacts(request: EvidenceRunRequest) -> tuple[ArtifactRef, ...]:
    candidates: list[ArtifactRef | None] = [
        request.candidate.estimation.calculation_artifact,
        request.hardware_passport_artifact,
        request.model_profile_artifact,
        request.requirements_artifact,
        request.workload_artifact,
        request.logs_artifact,
        *request.trial.evidence,
    ]
    if request.candidate.model_ref.type == "artifact":
        candidates.append(request.candidate.model_ref.artifact)
    if request.benchmark_result is not None:
        candidates.append(request.benchmark_result.provenance.raw_artifact)
    unique: dict[str, ArtifactRef] = {}
    for artifact in candidates:
        if artifact is not None:
            unique.setdefault(str(artifact.artifact_id), artifact)
    return tuple(unique.values())


def _milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1_000)


def _timestamp(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1_000, UTC)


__all__ = ["MlflowTrackingAdapter", "MlflowTrackingClient"]
