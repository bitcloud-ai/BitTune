from typing import Literal

from autopilot.domain.base import StrictModel
from autopilot.domain.hashing import (
    canonical_json_bytes,
    compute_content_hash,
    compute_plan_hash,
    verify_plan_hash,
)
from autopilot.domain.identifiers import ImageDigest, ModelRevision

SHA_A = "a" * 64
SHA_B = "b" * 64


class HashSpecification(StrictModel):
    schema_version: Literal["deployment-plan/v1"] = "deployment-plan/v1"
    adapter_version: str
    model_revision: ModelRevision
    image_digest: ImageDigest
    parameters: dict[str, int]


def make_specification(**changes: object) -> HashSpecification:
    values: dict[str, object] = {
        "adapter_version": "1.0.0",
        "model_revision": ModelRevision(root="1" * 40),
        "image_digest": ImageDigest(root=f"vllm/vllm-openai@sha256:{SHA_A}"),
        "parameters": {"max_num_seqs": 8, "max_num_batched_tokens": 4096},
    }
    values.update(changes)
    return HashSpecification.model_validate(values)


def test_canonical_json_is_independent_of_mapping_insertion_order() -> None:
    first = make_specification(parameters={"max_num_seqs": 8, "max_num_batched_tokens": 4096})
    second = make_specification(parameters={"max_num_batched_tokens": 4096, "max_num_seqs": 8})

    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert compute_plan_hash(first) == compute_plan_hash(second)


def test_plan_hash_covers_adapter_revision_image_and_parameters() -> None:
    baseline = make_specification()
    baseline_hash = compute_plan_hash(baseline)
    variants = (
        make_specification(adapter_version="1.0.1"),
        make_specification(model_revision=ModelRevision(root="2" * 40)),
        make_specification(image_digest=ImageDigest(root=f"vllm/vllm-openai@sha256:{SHA_B}")),
        make_specification(parameters={"max_num_seqs": 16, "max_num_batched_tokens": 4096}),
    )

    assert all(compute_plan_hash(variant) != baseline_hash for variant in variants)
    assert verify_plan_hash(baseline, baseline_hash)
    assert compute_content_hash(baseline).root == baseline_hash.root
