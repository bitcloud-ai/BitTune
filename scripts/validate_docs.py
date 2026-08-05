"""Validate the architecture document manifest and structural report."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

DOCS_DIRECTORY: Final = Path("docs/llm-inference-autopilot-mvp-docs")
MANIFEST_NAME: Final = "manifest.json"
REPORT_NAME: Final = "validation-report.json"
MARKDOWN_LINK: Final = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


@dataclass(frozen=True, slots=True)
class DocumentFacts:
    """Computed facts for a single Markdown document."""

    file: str
    bytes: int
    lines: int
    sha256: str


def document_facts(path: Path) -> DocumentFacts:
    """Return deterministic byte, line, and digest facts for a document."""
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    return DocumentFacts(
        file=path.name,
        bytes=len(raw),
        lines=len(text.splitlines()),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def broken_readme_links(docs_directory: Path, readme_text: str) -> list[str]:
    """Return local README link targets that do not resolve inside the docs directory."""
    broken: list[str] = []
    for target in MARKDOWN_LINK.findall(readme_text):
        if target.startswith(("http://", "https://", "#")):
            continue
        relative_target = target.split("#", maxsplit=1)[0]
        if relative_target and not (docs_directory / relative_target).is_file():
            broken.append(target)
    return sorted(broken)


def unbalanced_code_fences(documents: list[Path]) -> list[str]:
    """Return documents with an odd number of fenced-code delimiters."""
    return sorted(
        path.name
        for path in documents
        if sum(line.startswith("```") for line in path.read_text(encoding="utf-8").splitlines()) % 2
    )


def validate_document_facts(
    facts: list[DocumentFacts], expected_by_name: dict[str, object]
) -> list[str]:
    """Compare computed facts with manifest entries."""
    errors: list[str] = []
    if {fact.file for fact in facts} != set(expected_by_name):
        errors.append("manifest document set does not match Markdown files")

    for fact in facts:
        expected = expected_by_name.get(fact.file)
        if not isinstance(expected, dict):
            continue
        if fact.bytes != expected["bytes"]:
            errors.append(f"{fact.file}: byte count mismatch")
        if fact.lines != expected["lines"]:
            errors.append(f"{fact.file}: line count mismatch")
        if fact.sha256 != expected["sha256"]:
            errors.append(f"{fact.file}: sha256 mismatch")
    return errors


def validate_report(
    facts: list[DocumentFacts],
    broken_links: list[str],
    bad_fences: list[str],
    report: dict[str, object],
) -> list[str]:
    """Compare computed structural checks with the checked-in validation report."""
    errors: list[str] = []
    expected_values: tuple[tuple[bool, str], ...] = (
        (broken_links == report["missing_readme_links"], "README link validation result is stale"),
        (bad_fences == report["unbalanced_code_fences"], "code-fence validation result is stale"),
        (sum(fact.bytes for fact in facts) == report["total_bytes"], "total_bytes is stale"),
        (sum(fact.lines for fact in facts) == report["total_lines"], "total_lines is stale"),
        ([fact.file for fact in facts] == report["documents_read"], "documents_read is stale"),
        (len(facts) == report["document_count"], "document_count is stale"),
    )
    errors.extend(message for condition, message in expected_values if not condition)

    checks = report.get("checks")
    if report.get("passed") is not True or not isinstance(checks, dict) or not all(checks.values()):
        errors.append("validation report contains a failed required check")
    return errors


def validate_repository(repository_root: Path) -> list[str]:
    """Validate checked-in document metadata against current files."""
    docs_directory = repository_root / DOCS_DIRECTORY
    manifest = json.loads((docs_directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    report = json.loads((docs_directory / REPORT_NAME).read_text(encoding="utf-8"))
    documents = sorted(docs_directory.glob("*.md"), key=lambda path: path.name)
    facts = [document_facts(path) for path in documents]
    expected_by_name = {item["file"]: item for item in manifest["documents"]}

    readme_text = (docs_directory / "README.md").read_text(encoding="utf-8")
    broken_links = broken_readme_links(docs_directory, readme_text)
    bad_fences = unbalanced_code_fences(documents)
    return validate_document_facts(facts, expected_by_name) + validate_report(
        facts, broken_links, bad_fences, report
    )


def main() -> int:
    """Run validation from the repository root."""
    repository_root = Path(__file__).resolve().parents[1]
    errors = validate_repository(repository_root)
    if errors:
        for error in errors:
            sys.stderr.write(f"{error}\n")
        return 1
    sys.stdout.write("architecture document manifest is consistent\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
