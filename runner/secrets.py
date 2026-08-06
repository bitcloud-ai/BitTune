# ruff: noqa: TRY003
"""systemd credential resolution at the privileged execution boundary."""

from __future__ import annotations

import os
from pathlib import Path

from runner.errors import RunnerConfigurationError, RunnerValidationError
from runner.logs import SecretRedactor
from runner.models import SecretRef


class SystemdCredentialResolver:
    """Resolve logical ``SecretRef`` values from ``CREDENTIALS_DIRECTORY``."""

    def __init__(
        self,
        credentials_directory: Path,
        *,
        redactor: SecretRedactor | None = None,
    ) -> None:
        if not credentials_directory.is_absolute():
            raise RunnerConfigurationError("systemd credential directory must be absolute")
        try:
            root = credentials_directory.resolve(strict=True)
        except OSError as error:
            raise RunnerConfigurationError("systemd credential directory does not exist") from error
        if not root.is_dir():
            raise RunnerConfigurationError("systemd credential directory must be a directory")
        self._root = root
        self._redactor = redactor

    @classmethod
    def from_environment(
        cls,
        *,
        redactor: SecretRedactor | None = None,
    ) -> SystemdCredentialResolver:
        configured = os.environ.get("CREDENTIALS_DIRECTORY")
        if not configured:
            raise RunnerConfigurationError("CREDENTIALS_DIRECTORY is not configured")
        return cls(Path(configured), redactor=redactor)

    def resolve(self, reference: SecretRef) -> bytes:
        """Read one credential without ever embedding its value in an error."""

        candidate = self._root / str(reference)
        if candidate.is_symlink():
            raise RunnerValidationError("systemd credential must not be a symbolic link")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise RunnerValidationError("referenced systemd credential does not exist") from error
        if resolved.parent != self._root or not resolved.is_file():
            raise RunnerValidationError("referenced systemd credential is outside its boundary")
        try:
            value = resolved.read_bytes()
        except OSError as error:
            raise RunnerValidationError("referenced systemd credential cannot be read") from error
        if self._redactor is not None:
            self._redactor.register(value)
        return value
