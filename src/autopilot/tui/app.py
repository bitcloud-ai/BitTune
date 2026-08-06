"""Textual continuous-conversation interface for BitTune Autopilot."""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Collapsible, Input, Markdown, Pretty, Static

from autopilot.tui.client import SessionEvent, TuiApiError, TuiClient


class AutopilotApp(App[None]):
    """Full-screen TUI backed only by the public session API."""

    TITLE = "BitTune Autopilot"
    ENABLE_COMMAND_PALETTE = True
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("ctrl+c", "quit", show=False),
        Binding("ctrl+n", "new_session", show=False),
    ]
    CSS = """
    Screen {
        layout: vertical;
        background: #101413;
        color: #e5ebe8;
    }

    #brand {
        height: 3;
        padding: 1 2 0 2;
        background: #171d1b;
        color: #8fe0b5;
        text-style: bold;
    }

    #timeline {
        height: 1fr;
        padding: 1 2;
        scrollbar-color: #4d6b5d;
        scrollbar-background: #171d1b;
    }

    .message-user {
        width: 100%;
        margin: 1 0 0 0;
        padding: 1 2;
        background: #22312b;
        color: #f3f7f5;
        border-left: solid #70c99a;
    }

    .message-assistant {
        width: 100%;
        margin: 1 0 0 0;
        padding: 0 1;
    }

    .tool-event {
        width: 100%;
        margin: 1 0 0 0;
        border-left: solid #5aa7b5;
        background: #162124;
    }

    .interrupt-event {
        width: 100%;
        margin: 1 0 0 0;
        padding: 1 2;
        background: #2d291b;
        color: #f2d37d;
        border-left: solid #d8ae42;
    }

    .error-event {
        width: 100%;
        margin: 1 0 0 0;
        padding: 1 2;
        background: #321e1e;
        color: #ffb4ab;
        border-left: solid #d96b64;
    }

    #session-status {
        height: 2;
        padding: 0 2;
        background: #171d1b;
        color: #aebbb5;
    }

    #prompt {
        height: 3;
        margin: 0 1 1 1;
        border: tall #4d6b5d;
        background: #101413;
    }

    #prompt:focus {
        border: tall #70c99a;
    }
    """

    def __init__(
        self,
        *,
        client: TuiClient,
        base_url: str,
        session_id: str | None = None,
    ) -> None:
        super().__init__()
        self._client = client
        self._base_url = base_url
        self.session_id = session_id
        self._busy = False
        self._interrupted = False
        self._assistant_buffer = ""
        self._assistant_widget: Markdown | None = None
        self._rendered_messages = 0

    def compose(self) -> ComposeResult:
        yield Static("BITTUNE / INFERENCE AUTOPILOT", id="brand")
        yield VerticalScroll(id="timeline")
        yield Static(id="session-status")
        yield Input(placeholder="Message BitTune", id="prompt", max_length=4096)

    def on_mount(self) -> None:
        self.query_one("#prompt", Input).focus()
        self._update_status("ready")
        if self.session_id is not None:
            self._load_session(render_history=True)

    async def on_unmount(self) -> None:
        await self._client.close()

    def get_system_commands(self, screen: Screen[object]) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        yield SystemCommand(
            "Approve pending action", "Resume an approved Agent action", self.action_approve
        )
        yield SystemCommand(
            "Reject pending action", "Reject the pending Agent action", self.action_reject
        )
        yield SystemCommand(
            "Refresh session", "Reload the current session status", self.action_status
        )
        yield SystemCommand(
            "Cancel experiment", "Request cancellation for this experiment", self.action_cancel
        )
        yield SystemCommand(
            "New session", "Start a separate Autopilot session", self.action_new_session
        )

    def _update_status(self, state: str, *, phase: str | None = None) -> None:
        session = self.session_id or "new"
        phase_text = f"  phase={phase}" if phase else ""
        self.query_one("#session-status", Static).update(
            Text(f"{state}  session={session}{phase_text}  api={self._base_url}")
        )

    async def _mount(self, widget: Static | Markdown | Collapsible) -> None:
        timeline = self.query_one("#timeline", VerticalScroll)
        await timeline.mount(widget)
        timeline.scroll_end(animate=False)

    async def _render_message(self, message: dict[str, object]) -> None:
        role = message.get("role")
        content = message.get("content")
        if not isinstance(content, str) or content == "[structured message]":
            return
        if role == "user":
            await self._mount(Static(Text(content), classes="message-user"))
        elif role == "assistant":
            await self._mount(Markdown(content, classes="message-assistant"))
        elif role == "tool":
            name = message.get("tool_name")
            await self._mount(
                Collapsible(
                    Static(Text(content)),
                    title=f"Tool result / {name or 'tool'}",
                    collapsed=True,
                    classes="tool-event",
                )
            )

    async def _render_session(self, payload: dict[str, object], *, history: bool) -> None:
        experiment_id = payload.get("experiment_id")
        if isinstance(experiment_id, str):
            self.session_id = experiment_id
        status = payload.get("status")
        phase = payload.get("phase")
        self._interrupted = payload.get("interrupted") is True
        if history:
            messages = payload.get("messages")
            if isinstance(messages, list):
                for message in messages[self._rendered_messages :]:
                    if isinstance(message, dict):
                        await self._render_message(message)
                self._rendered_messages = len(messages)
        self._update_status(
            str(status) if isinstance(status, str) else "ready",
            phase=str(phase) if isinstance(phase, str) else None,
        )

    async def _handle_event(self, event: SessionEvent) -> None:
        if event.event_type == "assistant.delta":
            delta = event.data.get("delta")
            if isinstance(delta, str):
                self._assistant_buffer += delta
                if self._assistant_widget is not None:
                    await self._assistant_widget.update(self._assistant_buffer)
        elif event.event_type == "tool.call":
            name = event.data.get("tool_name", "tool")
            await self._mount(
                Collapsible(
                    Pretty(event.data),
                    title=f"Tool call / {name}",
                    collapsed=True,
                    classes="tool-event",
                )
            )
        elif event.event_type == "tool.result":
            name = event.data.get("tool_name", "tool")
            await self._mount(
                Collapsible(
                    Pretty(event.data),
                    title=f"Tool result / {name}",
                    collapsed=True,
                    classes="tool-event",
                )
            )
        elif event.event_type == "agent.interrupt":
            self._interrupted = True
            await self._mount(
                Static(
                    Text(f"Approval required / {self._interrupt_action(event)}"),
                    classes="interrupt-event",
                )
            )
        elif event.event_type == "run.error":
            code = event.data.get("code", "AGENT_STREAM_FAILED")
            await self._mount(Static(Text(str(code)), classes="error-event"))
        elif event.event_type == "run.completed":
            session = event.data.get("session")
            if isinstance(session, dict):
                await self._render_session(session, history=False)
            if not self._assistant_buffer:
                message = event.data.get("assistant_message")
                if isinstance(message, str) and message and self._assistant_widget is not None:
                    self._assistant_buffer = message
                    await self._assistant_widget.update(message)

    @staticmethod
    def _interrupt_action(event: SessionEvent) -> str:
        requests = event.data.get("action_requests")
        if not isinstance(requests, list) or not requests or not isinstance(requests[0], dict):
            return "approval"
        candidate = requests[0].get("name")
        return candidate if isinstance(candidate, str) else "approval"

    async def _ensure_session(self) -> None:
        if self.session_id is not None:
            return
        payload = await self._client.create_session()
        await self._render_session(payload, history=False)
        if self.session_id is None:
            raise TuiApiError.missing_session_id()

    @work(exclusive=True, group="session-load")
    async def _load_session(self, *, render_history: bool) -> None:
        if self.session_id is None:
            return
        try:
            payload = await self._client.get_session(self.session_id)
            await self._render_session(payload, history=render_history)
        except TuiApiError as error:
            await self._mount(Static(Text(str(error)), classes="error-event"))

    @work(exclusive=True, group="agent-run")
    async def _send_message(self, message: str) -> None:
        self._busy = True
        prompt = self.query_one("#prompt", Input)
        prompt.disabled = True
        self._assistant_buffer = ""
        self._assistant_widget = Markdown("", classes="message-assistant")
        try:
            await self._ensure_session()
            await self._mount(Static(Text(message), classes="message-user"))
            await self._mount(self._assistant_widget)
            self._update_status("running")
            active_session_id = self.session_id
            if active_session_id is None:
                raise TuiApiError.missing_session_id()
            async for event in self._client.stream_message(active_session_id, message):
                await self._handle_event(event)
        except TuiApiError as error:
            await self._mount(Static(Text(str(error)), classes="error-event"))
            self._update_status("error")
        finally:
            self._busy = False
            prompt.disabled = False
            prompt.focus()

    @work(exclusive=True, group="agent-run")
    async def _resume(self, decision: str, message: str | None = None) -> None:
        if self.session_id is None or not self._interrupted:
            self.notify("No pending approval", severity="warning")
            return
        self._busy = True
        prompt = self.query_one("#prompt", Input)
        prompt.disabled = True
        self._assistant_buffer = ""
        self._assistant_widget = Markdown("", classes="message-assistant")
        try:
            await self._mount(self._assistant_widget)
            self._update_status("resuming")
            async for event in self._client.stream_resume(self.session_id, decision, message):
                await self._handle_event(event)
        except TuiApiError as error:
            await self._mount(Static(Text(str(error)), classes="error-event"))
            self._update_status("error")
        finally:
            self._busy = False
            prompt.disabled = False
            prompt.focus()

    @work(exclusive=True, group="control")
    async def _cancel(self) -> None:
        if self.session_id is None:
            self.notify("No active session", severity="warning")
            return
        try:
            await self._client.cancel_session(self.session_id)
            await self._load_session(render_history=False).wait()
        except TuiApiError as error:
            await self._mount(Static(Text(str(error)), classes="error-event"))

    @on(Input.Submitted, "#prompt")
    def submit_prompt(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.clear()
        if not text:
            return
        if text.startswith("/"):
            self._dispatch_command(text)
            return
        if self._busy:
            self.notify("Agent turn is still running", severity="warning")
            return
        self._send_message(text)

    def _dispatch_command(self, value: str) -> None:
        command, _, argument = value.partition(" ")
        command = command.casefold()
        if command == "/approve":
            self._resume("approve", argument or None)
        elif command == "/reject":
            self._resume("reject", argument or None)
        elif command == "/status":
            self.action_status()
        elif command == "/cancel":
            self.action_cancel()
        elif command == "/new":
            self.action_new_session()
        elif command in {"/quit", "/exit"}:
            self.exit()
        else:
            self.notify("Unknown command", severity="warning")

    def action_approve(self) -> None:
        self._resume("approve")

    def action_reject(self) -> None:
        self._resume("reject")

    def action_status(self) -> None:
        self._load_session(render_history=False)

    def action_cancel(self) -> None:
        self._cancel()

    def action_new_session(self) -> None:
        self._new_session()

    @work(exclusive=True, group="control")
    async def _new_session(self) -> None:
        if self._busy:
            self.notify("Agent turn is still running", severity="warning")
            return
        self.session_id = None
        self._interrupted = False
        self._rendered_messages = 0
        self._assistant_buffer = ""
        self._assistant_widget = None
        await self.query_one("#timeline", VerticalScroll).remove_children()
        self._update_status("ready")
        self.query_one("#prompt", Input).focus()


__all__ = ["AutopilotApp"]
