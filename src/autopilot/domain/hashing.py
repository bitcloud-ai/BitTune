"""RFC 8785 canonical serialization and immutable Plan hashing."""

import hashlib

import rfc8785
from pydantic import BaseModel

from autopilot.domain.identifiers import PlanHash, Sha256Digest


class PlanCanonicalizationError(ValueError):
    """Raised when a model cannot be represented as RFC 8785 JSON."""

    def __init__(self) -> None:
        super().__init__("model cannot be canonicalized")


def canonical_json_bytes(model: BaseModel) -> bytes:
    """Serialize a validated model using RFC 8785 JSON Canonicalization Scheme."""
    payload = model.model_dump(mode="json", by_alias=True, exclude_none=False)
    try:
        return rfc8785.dumps(payload)
    except (
        rfc8785.CanonicalizationError,
        rfc8785.FloatDomainError,
        rfc8785.IntegerDomainError,
    ) as exc:
        raise PlanCanonicalizationError from exc


def compute_content_hash(model: BaseModel) -> Sha256Digest:
    """Hash any validated immutable contract using canonical JSON."""
    digest = hashlib.sha256(canonical_json_bytes(model)).hexdigest()
    return Sha256Digest(root=f"sha256:{digest}")


def compute_plan_hash(execution_specification: BaseModel) -> PlanHash:
    """Hash the complete immutable execution specification."""
    return PlanHash(root=compute_content_hash(execution_specification).root)


def verify_plan_hash(execution_specification: BaseModel, expected: PlanHash) -> bool:
    """Compare a specification with an expected immutable Plan hash."""
    return compute_plan_hash(execution_specification) == expected
