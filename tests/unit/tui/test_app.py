from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from textual.widgets import Input, Markdown

from autopilot.tui.app import AutopilotApp
from autopilot.tui.client import SessionEvent

SESSION_ID = "exp_" + "1" * 32


class FakeTuiClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.closed = False

    async def create_session(self) -> dict[str, object]:
        return {
            "experiment_id": SESSION_ID,
            "status": "active",
            "phase": "requirements",
            "interrupted": False,
            "messages": [],
        }

    async def get_session(self, session_id: str) -> dict[str, object]:
        return {
            "experiment_id": session_id,
            "status": "active",
            "phase": "requirements",
            "interrupted": False,
            "messages": [],
        }

    async def cancel_session(self, session_id: str) -> dict[str, object]:
        return {"experiment_id": session_id, "status": "cancelled"}

    async def stream_message(
        self,
        session_id: str,
        message: str,
    ) -> AsyncIterator[SessionEvent]:
        self.messages.append((session_id, message))
        yield SessionEvent(event_type="assistant.delta", data={"delta": "Plan ready"})
        yield SessionEvent(
            event_type="run.completed",
            data={
                "session": {
                    "experiment_id": session_id,
                    "status": "active",
                    "phase": "planning",
                    "interrupted": False,
                }
            },
        )

    async def stream_resume(
        self,
        session_id: str,
        decision: str,
        message: str | None = None,
    ) -> AsyncIterator[SessionEvent]:
        del session_id, decision, message
        if False:
            yield SessionEvent(event_type="unused", data={})

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_tui_submits_input_and_renders_streamed_agent_text() -> None:
    client = FakeTuiClient()
    app = AutopilotApp(client=client, base_url="http://control-plane")

    async with app.run_test() as pilot:
        prompt = app.query_one("#prompt", Input)
        prompt.value = "inspect the GPU host"
        await pilot.press("enter")
        await app.workers.wait_for_complete()

        assert client.messages == [(SESSION_ID, "inspect the GPU host")]
        assistant = app.query(".message-assistant").last(Markdown)
        assert assistant.source == "Plan ready"
        assert "phase=planning" in str(app.query_one("#session-status").render())

    assert client.closed is True
