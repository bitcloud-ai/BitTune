"""Async REST/SSE transport used exclusively by the Textual presentation layer."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol

import httpx


class TuiApiError(RuntimeError):
    """Safe transport failure suitable for rendering in the terminal."""

    @classmethod
    def unavailable(cls) -> TuiApiError:
        return cls("Control plane is unavailable")

    @classmethod
    def stream_unavailable(cls) -> TuiApiError:
        return cls("Control plane stream is unavailable")

    @classmethod
    def http(cls, status_code: int, detail: str) -> TuiApiError:
        return cls(f"HTTP {status_code}: {detail}")

    @classmethod
    def invalid_json(cls) -> TuiApiError:
        return cls("Control plane returned invalid JSON")

    @classmethod
    def invalid_response(cls) -> TuiApiError:
        return cls("Control plane returned an invalid response")

    @classmethod
    def invalid_sse_json(cls) -> TuiApiError:
        return cls("Control plane returned invalid SSE JSON")

    @classmethod
    def invalid_sse_event(cls) -> TuiApiError:
        return cls("Control plane returned an invalid SSE event")

    @classmethod
    def missing_session_id(cls) -> TuiApiError:
        return cls("Control plane did not return a session ID")


@dataclass(frozen=True, slots=True)
class SessionEvent:
    event_type: str
    data: dict[str, object]


class TuiClient(Protocol):
    async def create_session(self) -> dict[str, object]: ...

    async def get_session(self, session_id: str) -> dict[str, object]: ...

    async def cancel_session(self, session_id: str) -> dict[str, object]: ...

    def stream_message(self, session_id: str, message: str) -> AsyncIterator[SessionEvent]: ...

    def stream_resume(
        self,
        session_id: str,
        decision: str,
        message: str | None = None,
    ) -> AsyncIterator[SessionEvent]: ...

    async def close(self) -> None: ...


class TuiApiClient:
    """Typed API client; it owns no workflow or authorization behavior."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout_seconds,
            transport=transport,
        )

    async def __aenter__(self) -> TuiApiClient:
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._client.__aexit__(exc_type, exc_value, traceback)

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except (TypeError, ValueError):
            return "API request failed"
        if not isinstance(payload, dict):
            return "API request failed"
        detail = payload.get("detail", payload)
        if isinstance(detail, dict):
            message = detail.get("message") or detail.get("code")
            if isinstance(message, str):
                return message
        return "API request failed"

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        try:
            response = await self._client.request(method, path, json=payload)
        except httpx.HTTPError as error:
            raise TuiApiError.unavailable() from error
        if not response.is_success:
            detail = self._error_detail(response)
            raise TuiApiError.http(response.status_code, detail)
        try:
            result = response.json()
        except (TypeError, ValueError) as error:
            raise TuiApiError.invalid_json() from error
        if not isinstance(result, dict):
            raise TuiApiError.invalid_response()
        return result

    async def create_session(self) -> dict[str, object]:
        return await self._request_json(
            "POST",
            "/api/v1/sessions",
            payload={"schema_version": "create-session-request/v1", "message": None},
        )

    async def get_session(self, session_id: str) -> dict[str, object]:
        return await self._request_json("GET", f"/api/v1/sessions/{session_id}")

    async def cancel_session(self, session_id: str) -> dict[str, object]:
        return await self._request_json("POST", f"/api/v1/experiments/{session_id}/cancel")

    async def _stream(
        self,
        path: str,
        payload: dict[str, object],
    ) -> AsyncIterator[SessionEvent]:
        try:
            async with self._client.stream("POST", path, json=payload) as response:
                if not response.is_success:
                    await response.aread()
                    detail = self._error_detail(response)
                    raise TuiApiError.http(response.status_code, detail)
                event_type = "message"
                data_lines: list[str] = []
                async for line in response.aiter_lines():
                    if not line:
                        if data_lines:
                            raw = "\n".join(data_lines)
                            try:
                                data = json.loads(raw)
                            except (TypeError, ValueError) as error:
                                raise TuiApiError.invalid_sse_json() from error
                            if not isinstance(data, dict):
                                raise TuiApiError.invalid_sse_event()
                            yield SessionEvent(event_type=event_type, data=data)
                        event_type = "message"
                        data_lines = []
                    elif line.startswith("event:"):
                        event_type = line.removeprefix("event:").strip()
                    elif line.startswith("data:"):
                        data_lines.append(line.removeprefix("data:").lstrip())
        except TuiApiError:
            raise
        except httpx.HTTPError as error:
            raise TuiApiError.stream_unavailable() from error

    async def stream_message(
        self,
        session_id: str,
        message: str,
    ) -> AsyncIterator[SessionEvent]:
        async for event in self._stream(
            f"/api/v1/sessions/{session_id}/messages/stream",
            {"schema_version": "session-message-request/v1", "message": message},
        ):
            yield event

    async def stream_resume(
        self,
        session_id: str,
        decision: str,
        message: str | None = None,
    ) -> AsyncIterator[SessionEvent]:
        async for event in self._stream(
            f"/api/v1/sessions/{session_id}/resume/stream",
            {
                "schema_version": "session-resume-request/v1",
                "decision": decision,
                "message": message,
            },
        ):
            yield event


__all__ = ["SessionEvent", "TuiApiClient", "TuiApiError", "TuiClient"]
