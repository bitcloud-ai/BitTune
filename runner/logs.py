"""Secret-safe runner logging and bounded container log excerpts."""

from __future__ import annotations

import logging
import re
from threading import Lock
from types import TracebackType
from typing import cast

from pydantic import Field

from runner.models import RunnerModel

REDACTION_MARKER = "[REDACTED]"
MAX_LOG_EXCERPT_CHARS = 65_536

_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|proxy-authorization|x-api-key|api[-_]?key|hf_token|"
    r"access_token|refresh_token|token|password|secret)\b(\s*[:=]\s*)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
)
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_KNOWN_TOKEN_SHAPE = re.compile(
    r"(?<![A-Za-z0-9])(?:hf_[A-Za-z0-9]{10,}|sk-[A-Za-z0-9_-]{16,})(?![A-Za-z0-9])"
)


class RedactedLogExcerpt(RunnerModel):
    """A log value that has crossed the runner's fixed redaction boundary."""

    text: str = Field(max_length=MAX_LOG_EXCERPT_CHARS)
    truncated: bool


class SecretRedactor:
    """Redact resolved credentials and fixed credential-bearing log syntax."""

    def __init__(self) -> None:
        self._secret_values: set[str] = set()
        self._mutex = Lock()

    def register(self, value: bytes | str) -> None:
        """Register one resolved secret without exposing it through this API."""

        if isinstance(value, bytes):
            try:
                decoded = value.decode("utf-8")
            except UnicodeDecodeError:
                return
        else:
            decoded = value
        if not decoded:
            return
        with self._mutex:
            self._secret_values.add(decoded)

    def redact(self, value: bytes | str) -> RedactedLogExcerpt:
        """Return a bounded excerpt with all known sensitive values masked."""

        text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
        with self._mutex:
            registered = tuple(sorted(self._secret_values, key=len, reverse=True))
        for secret in registered:
            text = text.replace(secret, REDACTION_MARKER)
        text = _SENSITIVE_ASSIGNMENT.sub(
            lambda match: f"{match.group(1)}{match.group(2)}{REDACTION_MARKER}",
            text,
        )
        text = _BEARER_VALUE.sub(f"Bearer {REDACTION_MARKER}", text)
        text = _KNOWN_TOKEN_SHAPE.sub(REDACTION_MARKER, text)
        truncated = len(text) > MAX_LOG_EXCERPT_CHARS
        if truncated:
            text = text[-MAX_LOG_EXCERPT_CHARS:]
        return RedactedLogExcerpt(text=text, truncated=truncated)


class RedactingFormatter(logging.Formatter):
    """Last logging boundary for runner-owned process and exception messages."""

    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self._redactor.redact(super().format(record)).text

    def formatException(self, exc_info: object) -> str:  # noqa: N802
        typed = cast(
            tuple[type[BaseException], BaseException, TracebackType | None]
            | tuple[None, None, None],
            exc_info,
        )
        return self._redactor.redact(super().formatException(typed)).text


def configure_runner_logging(redactor: SecretRedactor) -> None:
    """Install the one formatter used by the standalone Runner process."""

    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(redactor))
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)
