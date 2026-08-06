# ruff: noqa: TRY003
"""Resolution of logical storage references inside registered roots."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from runner.errors import PathBoundaryError, RunnerConfigurationError
from runner.models import StorageRef, StorageRoot


class RootRegistry:
    """Resolve runner storage references without accepting host paths from callers."""

    def __init__(self, roots: Mapping[StorageRoot, Path]) -> None:
        expected = set(StorageRoot)
        if set(roots) != expected:
            missing = sorted(root.value for root in expected - set(roots))
            extra = sorted(str(root) for root in set(roots) - expected)
            raise RunnerConfigurationError(
                f"runner roots must register every logical root; missing={missing}, extra={extra}"
            )

        normalized: dict[StorageRoot, Path] = {}
        for logical_root, configured in roots.items():
            if not configured.is_absolute():
                raise RunnerConfigurationError(
                    f"configured root {logical_root.value} must be absolute"
                )
            configured.mkdir(parents=True, exist_ok=True)
            resolved = configured.resolve(strict=True)
            if not resolved.is_dir():
                raise RunnerConfigurationError(
                    f"configured root {logical_root.value} must be a directory"
                )
            normalized[logical_root] = resolved

        if len(set(normalized.values())) != len(normalized):
            raise RunnerConfigurationError(
                "runner logical roots must resolve to distinct directories"
            )
        self._roots = normalized

    def root(self, logical_root: StorageRoot) -> Path:
        """Return a trusted configured root."""

        return self._roots[logical_root]

    def resolve(
        self,
        reference: StorageRef,
        *,
        must_exist: bool = False,
        require_directory: bool | None = None,
    ) -> Path:
        """Resolve a reference and reject traversal and symlink escapes."""

        root = self._roots[reference.root]
        candidate = root.joinpath(*reference.relative_path.parts())
        self._assert_existing_ancestors_within_root(root, candidate)

        if must_exist and not candidate.exists():
            raise PathBoundaryError("registered storage reference does not exist")

        if candidate.exists() or candidate.is_symlink():
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise PathBoundaryError(
                    "registered storage reference cannot be resolved"
                ) from error
            self._assert_within(root, resolved)
            if require_directory is True and not resolved.is_dir():
                raise PathBoundaryError("registered storage reference is not a directory")
            if require_directory is False and not resolved.is_file():
                raise PathBoundaryError("registered storage reference is not a file")
            return resolved

        if require_directory is False and must_exist:
            raise PathBoundaryError("registered storage file does not exist")
        return candidate

    def prepare_directory(self, reference: StorageRef) -> Path:
        """Create a directory after validating every existing ancestor."""

        candidate = self.resolve(reference)
        candidate.mkdir(parents=True, exist_ok=True)
        return self.resolve(reference, must_exist=True, require_directory=True)

    @classmethod
    def _assert_existing_ancestors_within_root(cls, root: Path, candidate: Path) -> None:
        current = candidate
        pending: list[Path] = []
        while current != root and not current.exists() and not current.is_symlink():
            pending.append(current)
            current = current.parent
        if current == current.parent and current != root:
            raise PathBoundaryError("storage reference escaped its registered root")
        try:
            resolved_parent = current.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise PathBoundaryError("storage reference ancestor cannot be resolved") from error
        cls._assert_within(root, resolved_parent)

        # Reject a symlink in a not-yet-created suffix after a concurrent change.
        for child in reversed(pending):
            if child.is_symlink():
                try:
                    cls._assert_within(root, child.resolve(strict=True))
                except (OSError, RuntimeError) as error:
                    raise PathBoundaryError(
                        "storage reference contains an invalid symlink"
                    ) from error

    @staticmethod
    def _assert_within(root: Path, candidate: Path) -> None:
        try:
            common = Path(os.path.commonpath((root, candidate)))
        except ValueError as error:
            raise PathBoundaryError("storage reference is on an unexpected filesystem") from error
        if common != root:
            raise PathBoundaryError("storage reference escaped its registered root")
