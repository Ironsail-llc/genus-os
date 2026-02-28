"""
RobothorApp — main Textual application for the Robothor TUI.

Connects to the running Engine daemon via SSE and provides a
streaming terminal chat interface.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Input

from robothor.tui.client import EngineClient
from robothor.tui.widgets import (
    MessageDisplay,
    StatusBar,
    ToolCard,
    WelcomeBanner,
)

logger = logging.getLogger(__name__)

CSS_PATH = Path(__file__).parent / "theme.tcss"


class RobothorApp(App):
    """Terminal chat interface for the Robothor Agent Engine."""

    TITLE = "Robothor"
    CSS_PATH = CSS_PATH

    BINDINGS = [
        Binding("escape", "abort", "Abort", show=True),
        Binding("ctrl+c", "quit", "Quit", show=True),
    ]

    def __init__(
        self,
        engine_url: str = "http://127.0.0.1:18800",
        session_key: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if session_key is None:
            username = getpass.getuser()
            session_key = f"agent:main:tui-{username}"
        self.client = EngineClient(base_url=engine_url, session_key=session_key)
        self._stream_task: asyncio.Task | None = None
        self._connected = False
        self._reconnect_task: asyncio.Task | None = None
        self._input_history: list[str] = []
        self._history_index = -1

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="chat-scroll")
        yield Input(placeholder="Type a message...", id="message-input")
        yield StatusBar(id="status-bar")
        yield Footer()

    @property
    def status_bar(self) -> StatusBar:
        return self.query_one("#status-bar", StatusBar)

    async def on_mount(self) -> None:
        """Connect to engine on startup."""
        self._reconnect_task = asyncio.create_task(self._reconnection_loop())
        await self._try_connect()

    async def _try_connect(self) -> None:
        """Attempt to connect to the engine and show appropriate banner."""
        health = await self.client.check_health()
        chat_scroll = self.query_one("#chat-scroll")

        if health:
            self._connected = True
            agent_count = len(health.get("agents", {}))
            self.status_bar.set_connected(True, agent_count)
            await chat_scroll.mount(WelcomeBanner(agent_count=agent_count))

            # Load existing history
            history = await self.client.get_history()
            for msg in history:
                role = msg.get("role", "assistant")
                content = msg.get("content", "")
                if role in ("user", "assistant") and content:
                    await chat_scroll.mount(MessageDisplay(content=content, role=role))
        else:
            self._connected = False
            self.status_bar.set_connected(False)
            await chat_scroll.mount(
                MessageDisplay(
                    content="Cannot connect to engine at "
                    f"{self.client.base_url}. Start it with: robothor engine start",
                    role="system",
                )
            )

        # Scroll to bottom
        chat_scroll.scroll_end(animate=False)

        # Focus input
        self.query_one("#message-input", Input).focus()

    async def _reconnection_loop(self) -> None:
        """Background task: retry /health every 5s when disconnected."""
        while True:
            await asyncio.sleep(5)
            if not self._connected:
                health = await self.client.check_health()
                if health:
                    self._connected = True
                    agent_count = len(health.get("agents", {}))
                    self.status_bar.set_connected(True, agent_count)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle user input — slash commands or chat messages."""
        text = event.value.strip()
        if not text:
            return

        input_widget = self.query_one("#message-input", Input)
        input_widget.value = ""

        # Save to input history
        self._input_history.append(text)
        self._history_index = -1

        # Try slash commands first
        if text.startswith("/"):
            from robothor.tui.commands import handle_command

            handled, output = await handle_command(self, text)
            if handled:
                if output:
                    chat_scroll = self.query_one("#chat-scroll")
                    await chat_scroll.mount(
                        MessageDisplay(content=output, role="system")
                    )
                    chat_scroll.scroll_end(animate=False)
                return

        if not self._connected:
            chat_scroll = self.query_one("#chat-scroll")
            await chat_scroll.mount(
                MessageDisplay(
                    content="Not connected to engine. Check /status.",
                    role="system",
                )
            )
            chat_scroll.scroll_end(animate=False)
            return

        # Show user message
        chat_scroll = self.query_one("#chat-scroll")
        await chat_scroll.mount(MessageDisplay(content=text, role="user"))
        chat_scroll.scroll_end(animate=False)

        # Start streaming response
        self._stream_task = asyncio.create_task(self._stream_response(text))

    async def _stream_response(self, message: str) -> None:
        """Stream a response from the engine, updating widgets in real-time."""
        chat_scroll = self.query_one("#chat-scroll")

        # Create the assistant message widget
        msg_widget = MessageDisplay(content="", role="assistant")
        await chat_scroll.mount(msg_widget)
        chat_scroll.scroll_end(animate=False)

        accumulated = ""
        tool_cards: dict[str, ToolCard] = {}

        try:
            async for event in self.client.send_message(message):
                if event.event == "delta":
                    accumulated += event.data.get("text", "")
                    msg_widget.update_content(accumulated)
                    chat_scroll.scroll_end(animate=False)

                elif event.event == "tool_start":
                    card = ToolCard(
                        tool_name=event.data.get("tool", "?"),
                        args=event.data.get("args", {}),
                        call_id=event.data.get("call_id", ""),
                    )
                    call_id = event.data.get("call_id", "")
                    tool_cards[call_id] = card
                    await chat_scroll.mount(card)
                    chat_scroll.scroll_end(animate=False)

                elif event.event == "tool_end":
                    call_id = event.data.get("call_id", "")
                    card = tool_cards.get(call_id)
                    if card:
                        error = event.data.get("error")
                        if error:
                            card.mark_error(error)
                        else:
                            card.mark_complete(event.data.get("duration_ms", 0))

                elif event.event == "done":
                    # Update with final text if we missed any deltas
                    final_text = event.data.get("text", "")
                    if final_text and final_text != accumulated:
                        msg_widget.update_content(final_text)

                    # Update status bar with model info
                    self.status_bar.set_model_info(
                        model=event.data.get("model"),
                        input_tokens=event.data.get("input_tokens", 0),
                        output_tokens=event.data.get("output_tokens", 0),
                    )

                elif event.event == "error":
                    error_text = event.data.get("error", "Unknown error")
                    msg_widget.update_content(f"[red]Error: {error_text}[/red]")

        except asyncio.CancelledError:
            msg_widget.update_content(accumulated + "\n[dim](aborted)[/dim]")
        except Exception as e:
            msg_widget.update_content(f"[red]Connection error: {e}[/red]")
            self._connected = False
            self.status_bar.set_connected(False)
        finally:
            self._stream_task = None

    async def action_abort(self) -> None:
        """Cancel the running stream and tell the engine."""
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
            await self.client.abort()

    async def on_key(self, event) -> None:
        """Handle up/down arrow for input history."""
        input_widget = self.query_one("#message-input", Input)
        if not input_widget.has_focus:
            return

        if event.key == "up" and self._input_history:
            if self._history_index == -1:
                self._history_index = len(self._input_history) - 1
            elif self._history_index > 0:
                self._history_index -= 1
            input_widget.value = self._input_history[self._history_index]
            input_widget.cursor_position = len(input_widget.value)
            event.prevent_default()

        elif event.key == "down" and self._input_history:
            if self._history_index == -1:
                return
            elif self._history_index < len(self._input_history) - 1:
                self._history_index += 1
                input_widget.value = self._input_history[self._history_index]
            else:
                self._history_index = -1
                input_widget.value = ""
            input_widget.cursor_position = len(input_widget.value)
            event.prevent_default()

    async def on_unmount(self) -> None:
        """Clean up on exit."""
        if self._reconnect_task:
            self._reconnect_task.cancel()
        if self._stream_task:
            self._stream_task.cancel()
        await self.client.close()
