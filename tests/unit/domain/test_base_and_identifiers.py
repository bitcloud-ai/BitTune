from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from autopilot.domain.base import StrictModel, UtcDatetime
from autopilot.domain.budgets import ExecutionBudget
from autopilot.domain.candidates import VllmTuningSpec
from autopilot.domain.constraints import ObjectiveSpec
from autopilot.domain.enums import NumericMetric, ObjectiveDirection
from autopilot.domain.hardware import NvidiaAccelerator
from autopilot.domain.identifiers import (
    ExperimentId,
    ImageDigest,
    ModelRevision,
    SecretRef,
    ToolName,
)


class TimestampedValue(StrictModel):
    captured_at: UtcDatetime


def test_strict_models_reject_type_coercion() -> None:
    budget = {
        "max_duration_seconds": 60,
        "max_requests": 5,
        "max_input_tokens": 100,
        "max_output_tokens": 100,
        "max_disk_growth_bytes": 1_000,
    }

    with pytest.raises(ValidationError, match="max_requests"):
        ExecutionBudget(**{**budget, "max_requests": "5"})
    with pytest.raises(ValidationError, match="max_duration_seconds"):
        ExecutionBudget(**{**budget, "max_duration_seconds": True})
    with pytest.raises(ValidationError, match="enable_chunked_prefill"):
        VllmTuningSpec(
            max_model_len=8_192,
            gpu_memory_utilization=0.9,
            max_num_seqs=8,
            max_num_batched_tokens=4_096,
            enable_chunked_prefill="false",
        )
    with pytest.raises(ValidationError, match="power_watts"):
        NvidiaAccelerator(
            name="NVIDIA GeForce RTX 5090",
            uuid="GPU-1234567890abcdef",
            memory_total_bytes=32_000_000_000,
            memory_free_bytes=31_000_000_000,
            temperature_celsius=30,
            utilization_percent=0,
            power_watts="123.4",
        )


def test_literal_enum_accepts_explicit_enum_instance() -> None:
    objective = ObjectiveSpec(
        metric=NumericMetric.SUCCESSFUL_OUTPUT_TOKENS_PER_SECOND,
        direction=ObjectiveDirection.MAXIMIZE,
    )

    assert objective.direction is ObjectiveDirection.MAXIMIZE


def test_strict_model_rejects_extra_fields_and_is_frozen() -> None:
    value = TimestampedValue(captured_at=datetime.now(UTC))

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TimestampedValue(captured_at=datetime.now(UTC), unknown=True)

    with pytest.raises(ValidationError, match="Instance is frozen"):
        value.captured_at = datetime.now(UTC)


def test_utc_datetime_normalizes_aware_offsets() -> None:
    source = datetime(2026, 8, 5, 20, 0, tzinfo=timezone(timedelta(hours=8)))

    value = TimestampedValue(captured_at=source)

    assert value.captured_at == datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def test_utc_datetime_rejects_naive_values() -> None:
    with pytest.raises(ValidationError, match="timezone info"):
        TimestampedValue(captured_at=datetime(2026, 8, 5, 12, 0))  # noqa: DTZ001


def test_stable_id_generation_preserves_resource_type() -> None:
    identifier = ExperimentId.new()

    assert identifier.root.startswith("exp_")
    assert len(identifier.root) == len("exp_") + 32


@pytest.mark.parametrize(
    "tool_name",
    [
        "create_benchmark_plan",
        "preview_deployment",
        "start_benchmark",
        "get_benchmark_status",
        "get_benchmark_result",
        "cancel_benchmark",
    ],
)
def test_tool_name_accepts_only_canonical_action_forms(tool_name: str) -> None:
    assert ToolName(root=tool_name).root == tool_name


@pytest.mark.parametrize(
    "tool_name",
    ["execute_shell", "inspect_environment", "get_logs", "request_approval", "docker_run"],
)
def test_tool_name_rejects_noncanonical_or_forbidden_actions(tool_name: str) -> None:
    with pytest.raises(ValidationError, match="allowed domain action"):
        ToolName(root=tool_name)


def test_image_digest_rejects_tags_without_digest() -> None:
    with pytest.raises(ValidationError, match="immutable"):
        ImageDigest(root="vllm/vllm-openai:latest")


def test_model_revision_rejects_floating_revision() -> None:
    with pytest.raises(ValidationError, match="commit hash"):
        ModelRevision(root="main")


@pytest.mark.parametrize("value", ["/run/secrets/key", "api_key", "ab", "TOKEN=value"])
def test_secret_ref_rejects_paths_values_and_invalid_names(value: str) -> None:
    with pytest.raises(ValidationError, match="logical kebab-case"):
        SecretRef(root=value)
