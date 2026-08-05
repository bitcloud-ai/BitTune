from pathlib import Path

from scripts.validate_docs import broken_readme_links, document_facts, unbalanced_code_fences


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
