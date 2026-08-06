"""Remote OpenAI-compatible ModelProvider used only by LLM graph nodes."""

from __future__ import annotations

from typing import Protocol, TypeVar

from openai import APIError, APITimeoutError, OpenAI
from pydantic import Field, SecretStr

from autopilot.domain.base import LongText, StrictModel
from autopilot.domain.errors import ErrorEnvelope
from autopilot.domain.requirements import RequirementSpec

StructuredT = TypeVar("StructuredT", bound=StrictModel)


class BenchmarkIntent(StrictModel):
    """Structured test strategy proposed by the remote model."""

    schema_version: str = "benchmark-intent/v1"
    profiles: tuple[str, ...] = Field(min_length=1, max_length=4)
    rationale: LongText


class FailureAnalysis(StrictModel):
    """Provider explanation of a typed failure, never the source of truth."""

    schema_version: str = "failure-analysis/v1"
    summary: LongText
    actions: tuple[LongText, ...] = Field(min_length=1, max_length=8)


class ReportDraft(StrictModel):
    """A report draft is kept outside Graph State and published as an Artifact."""

    schema_version: str = "report-draft/v1"
    markdown: str = Field(min_length=1, max_length=64_000)


class ModelProvider(Protocol):
    """Narrow structured interface; implementations must not touch the GPU."""

    def parse_requirements(self, message: str) -> RequirementSpec: ...

    def propose_test_strategy(self, requirements: RequirementSpec) -> BenchmarkIntent: ...

    def analyze_failure(self, error: ErrorEnvelope) -> FailureAnalysis: ...

    def write_report(self, evidence_refs: tuple[str, ...]) -> ReportDraft: ...


class ModelProviderError(RuntimeError):
    """Safe provider failure without retaining raw response or credentials."""

    code = "MODEL_PROVIDER_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("remote ModelProvider request failed")


class OpenAICompatibleModelProvider:
    """Official OpenAI SDK adapter for a configured remote endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        model: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._client = OpenAI(
            base_url=base_url.rstrip("/"),
            api_key=api_key.get_secret_value(),
            timeout=timeout_seconds,
            max_retries=0,
        )
        self._model = model

    def _parse(self, instruction: str, schema: type[StructuredT]) -> StructuredT:
        try:
            completion = self._client.chat.completions.parse(
                model=self._model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Return only the requested structured object. Do not include secrets, "
                            "paths, shell commands, provider CLI flags, or measured metrics."
                        ),
                    },
                    {"role": "user", "content": instruction},
                ],
                response_format=schema,
            )
        except (APITimeoutError, APIError) as error:
            raise ModelProviderError from error
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            raise ModelProviderError
        return parsed

    def parse_requirements(self, message: str) -> RequirementSpec:
        return self._parse(message, RequirementSpec)

    def propose_test_strategy(self, requirements: RequirementSpec) -> BenchmarkIntent:
        return self._parse(requirements.model_dump_json(), BenchmarkIntent)

    def analyze_failure(self, error: ErrorEnvelope) -> FailureAnalysis:
        return self._parse(error.model_dump_json(), FailureAnalysis)

    def write_report(self, evidence_refs: tuple[str, ...]) -> ReportDraft:
        return self._parse("Evidence references:\n" + "\n".join(evidence_refs), ReportDraft)


__all__ = [
    "BenchmarkIntent",
    "FailureAnalysis",
    "ModelProvider",
    "ModelProviderError",
    "OpenAICompatibleModelProvider",
    "ReportDraft",
]
