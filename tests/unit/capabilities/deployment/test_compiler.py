import json
from pathlib import Path

import pytest

from autopilot.capabilities.deployment.application.compiler import compile_deployment
from autopilot.capabilities.deployment.application.service import preview_deployment
from autopilot.capabilities.deployment.domain.enums import DeploymentValidationCode
from autopilot.capabilities.deployment.domain.errors import DeploymentValidationError
from autopilot.capabilities.deployment.domain.models import (
    DeploymentExecutionSpecification,
    VllmVersionProfile,
)
from autopilot.domain.enums import RiskLevel
from autopilot.domain.identifiers import ImageDigest

GOLDEN_PATH = (
    Path(__file__).parents[4]
    / "src"
    / "autopilot"
    / "capabilities"
    / "deployment"
    / "tests"
    / "golden"
    / "vllm-balanced.expected.json"
)


def test_compile_deployment_matches_golden(
    deployment_specification: DeploymentExecutionSpecification,
    vllm_profile: VllmVersionProfile,
) -> None:
    compiled = compile_deployment(deployment_specification, vllm_profile)
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

    assert compiled.model_dump(mode="json") == expected


def test_preview_is_read_only_l2_approval_material(
    deployment_specification: DeploymentExecutionSpecification,
    vllm_profile: VllmVersionProfile,
) -> None:
    preview = preview_deployment(deployment_specification, vllm_profile)

    assert preview.risk_level is RiskLevel.L2
    assert preview.requires_human_approval is True


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"provider_version": "other"}, DeploymentValidationCode.VERSION_MISMATCH),
        (
            {"engine_image": ImageDigest(root=f"vllm/vllm-openai@sha256:{'d' * 64}")},
            DeploymentValidationCode.IMAGE_MISMATCH,
        ),
        ({"max_task_timeout_seconds": 600}, DeploymentValidationCode.BUDGET_EXCEEDED),
    ],
)
def test_compiler_fails_closed_on_unverified_context(
    deployment_specification: DeploymentExecutionSpecification,
    vllm_profile: VllmVersionProfile,
    change: dict[str, object],
    code: DeploymentValidationCode,
) -> None:
    profile = vllm_profile.model_copy(update=change)

    with pytest.raises(DeploymentValidationError) as caught:
        compile_deployment(deployment_specification, profile)

    assert caught.value.code is code


def test_compiler_rejects_plan_budget_shorter_than_startup_timeout(
    deployment_specification: DeploymentExecutionSpecification,
    vllm_profile: VllmVersionProfile,
) -> None:
    short_budget = deployment_specification.budget.model_copy(update={"max_duration_seconds": 300})
    specification = deployment_specification.model_copy(update={"budget": short_budget})

    with pytest.raises(DeploymentValidationError) as caught:
        compile_deployment(specification, vllm_profile)

    assert caught.value.code is DeploymentValidationCode.BUDGET_EXCEEDED


def test_compiler_rejects_unschedulable_full_prefill(
    deployment_specification: DeploymentExecutionSpecification,
    vllm_profile: VllmVersionProfile,
) -> None:
    parameters = deployment_specification.candidate.parameters.model_copy(
        update={
            "max_num_batched_tokens": 2_048,
            "enable_chunked_prefill": False,
        }
    )
    candidate = deployment_specification.candidate.model_copy(update={"parameters": parameters})
    specification = deployment_specification.model_copy(update={"candidate": candidate})

    with pytest.raises(DeploymentValidationError) as caught:
        compile_deployment(specification, vllm_profile)

    assert caught.value.code is DeploymentValidationCode.PARAMETER_UNSUPPORTED


def test_compiled_contract_has_no_arbitrary_docker_surface(
    deployment_specification: DeploymentExecutionSpecification,
    vllm_profile: VllmVersionProfile,
) -> None:
    payload = compile_deployment(deployment_specification, vllm_profile).model_dump(mode="json")
    serialized = json.dumps(payload)

    assert all(
        forbidden not in serialized
        for forbidden in (
            "command",
            "entrypoint",
            "environment",
            "volume",
            "host_path",
            "network_mode",
            "privileged",
            "docker.sock",
        )
    )
