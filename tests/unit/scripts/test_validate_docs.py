from pathlib import Path

from scripts.validate_docs import (
    broken_readme_links,
    document_facts,
    unbalanced_code_fences,
    validate_schema_examples,
)


def test_document_facts_use_raw_utf8_bytes(tmp_path: Path) -> None:
    document = tmp_path / "example.md"
    document.write_text("# Title\n\ncontent\n", encoding="utf-8", newline="\n")

    facts = document_facts(document)

    assert facts.file == "example.md"
    assert facts.bytes == len(document.read_bytes())
    assert facts.lines == 3
    assert len(facts.sha256) == 64


def test_broken_readme_links_ignore_remote_and_anchor_targets(tmp_path: Path) -> None:
    (tmp_path / "present.md").write_text("present", encoding="utf-8")
    readme = "[present](present.md) [missing](missing.md) [web](https://example.com) [top](#top)"

    assert broken_readme_links(tmp_path, readme) == ["missing.md"]


def test_unbalanced_code_fences_reports_only_invalid_documents(tmp_path: Path) -> None:
    balanced = tmp_path / "balanced.md"
    balanced.write_text("```text\nvalue\n```\n", encoding="utf-8", newline="\n")
    unbalanced = tmp_path / "unbalanced.md"
    unbalanced.write_text("```text\nvalue\n", encoding="utf-8", newline="\n")

    assert unbalanced_code_fences([balanced, unbalanced]) == ["unbalanced.md"]


def test_annotated_json_example_is_validated_against_registered_schema(tmp_path: Path) -> None:
    document = tmp_path / "example.md"
    document.write_text(
        "\n".join(
            (
                "<!-- schema-example: artifact-ref-v1 -->",
                "```json",
                '{"artifact_id":"artifact_11111111111111111111111111111111",'
                '"sha256":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
                '"content_type":"application/json","size_bytes":1,'
                '"producer":{"component":"test","version":"1.0.0"}}',
                "```",
                "",
            )
        ),
        encoding="utf-8",
        newline="\n",
    )

    result = validate_schema_examples([document])

    assert result.count == 1
    assert result.errors == ()


def test_annotated_json_example_reports_contract_drift(tmp_path: Path) -> None:
    document = tmp_path / "example.md"
    document.write_text(
        "<!-- schema-example: workload-v1 -->\n```json\n{}\n```\n",
        encoding="utf-8",
        newline="\n",
    )

    result = validate_schema_examples([document])

    assert result.count == 1
    assert result.errors == ("example.md:1: example does not match generated schema 'workload-v1'",)


def test_unannotated_provider_example_is_not_schema_validated(tmp_path: Path) -> None:
    document = tmp_path / "provider.md"
    document.write_text(
        '```json\n{"provider_specific": true}\n```\n',
        encoding="utf-8",
        newline="\n",
    )

    result = validate_schema_examples([document])

    assert result.count == 0
    assert result.errors == ()
