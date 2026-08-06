"""Strict Agent inputs owned by the evidence capability."""

from typing import Literal

from autopilot.domain.base import StrictModel
from autopilot.domain.identifiers import JobId


class EvidenceQueryInput(StrictModel):
    schema_version: Literal["evidence-query-input/v1"] = "evidence-query-input/v1"
    job_id: JobId


__all__ = ["EvidenceQueryInput"]
