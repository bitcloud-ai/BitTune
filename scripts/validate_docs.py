"""Validate architecture documents, their metadata, and annotated JSON examples."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from scripts.export_schemas import SCHEMA_MODELS

DOCS_DIRECTORY: Final = Path("docs/llm-inference-autopilot-mvp-docs")
MANIFEST_NAME: Final = "manifest.json"
REPORT_NAME: Final = "validation-report.json"
MARKDOWN_LINK: Final = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
SCHEMA_EXAMPLE_MARKER: Final = re.compile(r"<!-- schema-example: (?P<schema>[a-z][a-z0-9-]*) -->")


@dataclass(frozen=True, slots=True)
class DocumentFacts:
    """Computed facts for a single Markdown document."""

    file: str
    bytes: int
    lines: int
    sha256: str


@dataclass(frozen=True, slots=True)
class SchemaExampleValidation:
    """Validation facts for explicitly annotated public-contract examples."""

    count: int
    errors: tuple[str, ...]


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


def validate_schema_examples(documents: list[Path]) -> SchemaExampleValidation:
    """Validate JSON fences immediately following a public-schema marker."""
    errors: list[str] = []
    count = 0
    for path in documents:
        lines = path.read_text(encoding="utf-8").splitlines()
        line_index = 0
        while line_index < len(lines):
            marker = SCHEMA_EXAMPLE_MARKER.fullmatch(lines[line_index])
            if marker is None:
                line_index += 1
                continue

            count += 1
            marker_line = line_index + 1
            schema_name = marker.group("schema")
            if line_index + 1 >= len(lines) or lines[line_index + 1] != "```json":
                errors.append(
                    f"{path.name}:{marker_line}: schema example marker must immediately precede "
                    "a json fence"
                )
                line_index += 1
                continue

            closing_index = line_index + 2
            while closing_index < len(lines) and lines[closing_index] != "```":
                closing_index += 1
            if closing_index == len(lines):
                errors.append(f"{path.name}:{marker_line}: schema example json fence is unclosed")
                break

            model = SCHEMA_MODELS.get(schema_name)
            if model is None:
                errors.append(
                    f"{path.name}:{marker_line}: unknown generated schema {schema_name!r}"
                )
                line_index = closing_index + 1
                continue

            payload = "\n".join(lines[line_index + 2 : closing_index])
            try:
                model.model_validate_json(payload)
            except (ValidationError, ValueError):
                errors.append(
                    f"{path.name}:{marker_line}: example does not match generated schema "
                    f"{schema_name!r}"
                )
            line_index = closing_index + 1
    return SchemaExampleValidation(count=count, errors=tuple(errors))


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
    schema_examples: SchemaExampleValidation,
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
        (
            schema_examples.count == report.get("annotated_schema_example_count"),
            "annotated_schema_example_count is stale",
        ),
        (
            list(schema_examples.errors) == report.get("invalid_schema_examples"),
            "invalid_schema_examples is stale",
        ),
    )
    errors.extend(message for condition, message in expected_values if not condition)

    checks = report.get("checks")
    if report.get("passed") is not True or not isinstance(checks, dict) or not all(checks.values()):
        errors.append("validation report contains a failed required check")
    return errors


def updated_manifest(manifest: dict[str, object], facts: list[DocumentFacts]) -> dict[str, object]:
    """Return manifest metadata with current deterministic document facts."""
    updated = dict(manifest)
    updated["documents"] = [asdict(fact) for fact in facts]
    return updated


def updated_report(
    report: dict[str, object],
    facts: list[DocumentFacts],
    broken_links: list[str],
    bad_fences: list[str],
    schema_examples: SchemaExampleValidation,
) -> dict[str, object]:
    """Return structural report metadata computed from current documents."""
    updated = dict(report)
    raw_checks = report.get("checks")
    checks: dict[str, object] = {}
    if isinstance(raw_checks, dict):
        checks = {key: value for key, value in raw_checks.items() if isinstance(key, str)}
    checks["annotated_schema_examples_valid"] = not schema_examples.errors
    updated.update(
        {
            "documents_read": [fact.file for fact in facts],
            "document_count": len(facts),
            "total_lines": sum(fact.lines for fact in facts),
            "total_bytes": sum(fact.bytes for fact in facts),
            "missing_readme_links": broken_links,
            "unbalanced_code_fences": bad_fences,
            "annotated_schema_example_count": schema_examples.count,
            "invalid_schema_examples": list(schema_examples.errors),
            "checks": checks,
            "passed": not broken_links
            and not bad_fences
            and not schema_examples.errors
            and all(checks.values()),
        }
    )
    return updated


def write_json(path: Path, payload: dict[str, object]) -> None:
    """Write stable, human-reviewable JSON metadata."""
    path.write_text(
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n",
        encoding="utf-8",
        newline="\n",
    )


def document_state(
    docs_directory: Path,
) -> tuple[list[DocumentFacts], list[str], list[str], SchemaExampleValidation]:
    """Compute all deterministic checks shared by writing and verification."""
    documents = sorted(docs_directory.glob("*.md"), key=lambda path: path.name)
    facts = [document_facts(path) for path in documents]
    readme_text = (docs_directory / "README.md").read_text(encoding="utf-8")
    return (
        facts,
        broken_readme_links(docs_directory, readme_text),
        unbalanced_code_fences(documents),
        validate_schema_examples(documents),
    )


def refresh_metadata(repository_root: Path) -> None:
    """Refresh checked-in manifest and validation report from current documents."""
    docs_directory = repository_root / DOCS_DIRECTORY
    manifest_path = docs_directory / MANIFEST_NAME
    report_path = docs_directory / REPORT_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    facts, broken_links, bad_fences, schema_examples = document_state(docs_directory)
    write_json(manifest_path, updated_manifest(manifest, facts))
    write_json(
        report_path,
        updated_report(report, facts, broken_links, bad_fences, schema_examples),
    )


def validate_repository(repository_root: Path) -> list[str]:
    """Validate checked-in document metadata against current files."""
    docs_directory = repository_root / DOCS_DIRECTORY
    manifest = json.loads((docs_directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    report = json.loads((docs_directory / REPORT_NAME).read_text(encoding="utf-8"))
    facts, broken_links, bad_fences, schema_examples = document_state(docs_directory)
    expected_by_name = {item["file"]: item for item in manifest["documents"]}
    return (
        list(schema_examples.errors)
        + validate_document_facts(facts, expected_by_name)
        + validate_report(facts, broken_links, bad_fences, schema_examples, report)
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="refresh generated document metadata")
    return parser.parse_args()


def main() -> int:
    """Run validation from the repository root."""
    args = parse_args()
    repository_root = Path(__file__).resolve().parents[1]
    if args.write:
        refresh_metadata(repository_root)
    errors = validate_repository(repository_root)
    if errors:
        for error in errors:
            sys.stderr.write(f"{error}\n")
        return 1
    sys.stdout.write("architecture document manifest is consistent\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
