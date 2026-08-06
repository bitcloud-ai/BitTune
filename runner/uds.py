# ruff: noqa: TRY003
"""Unix Domain Socket path policy for the typed runner API.

FastAPI and Uvicorn provide the HTTP transport. This module only owns the
filesystem boundary for the socket, preventing a TCP fallback or path escape.
"""

from __future__ import annotations

import stat
from pathlib import Path

from runner.errors import RunnerConfigurationError


class UnixSocketEndpoint:
    """Trusted UDS endpoint rooted in the configured runtime directory."""

    def __init__(self, *, runtime_root: Path, socket_name: str = "runner.sock") -> None:
        if not runtime_root.is_absolute():
            raise RunnerConfigurationError("runner socket root must be absolute")
        if socket_name != "runner.sock":
            raise RunnerConfigurationError("runner socket name is fixed to runner.sock")
        runtime_root.mkdir(parents=True, exist_ok=True)
        self._runtime_root = runtime_root.resolve(strict=True)
        self._path = self._runtime_root / socket_name
        if self._path.parent != self._runtime_root:
            raise RunnerConfigurationError("runner socket escaped its runtime root")

    @property
    def path(self) -> Path:
        return self._path

    def remove_stale_socket(self) -> None:
        """Remove only an existing Unix socket at the configured exact path."""

        try:
            metadata = self._path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(metadata.st_mode):
            raise RunnerConfigurationError("runner socket path exists and is not a Unix socket")
        self._path.unlink()
