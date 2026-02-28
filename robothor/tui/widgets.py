"""
Custom Textual widgets for the Robothor TUI.

MessageDisplay — renders user/assistant chat messages with markup.
ToolCard — inline status card for tool calls (running → complete → error).
StatusBar — bottom bar with connection state, model, and token counts.
WelcomeBanner — startup banner with agent count and commands hint.
"""

from __future__ import annotations

from textual.content import Content
from textual.widgets import Static


class MessageDisplay(Static):
    """Renders a single chat message with role-based styling.

    User messages get a cyan prefix. Assistant messages render content directly.
    Supports live streaming updates via update_content().
    """

    def __init__(
        self,
        content: str = "",
        role: str = "assistant",
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        self._role = role
        self._content = content
        super().__init__(
            Content.from_markup(self._format()),
            name=name,
            id=id,
            classes=classes,
        )

    def _format(self) -> str:
        if self._role == "user":
            return f"[cyan]> {self._content}[/cyan]"
        elif self._role == "system":
            return f"[dim italic]{self._content}[/dim italic]"
        else:
            return self._content

    def update_content(self, text: str) -> None:
        """Update the displayed content (used during streaming)."""
        self._content = text
        self.update(Content.from_markup(self._format()))


class ToolCard(Static):
    """Inline card showing tool execution status.

    States:
    - running: "> tool_name(args) running..."
    - complete: "v tool_name(args) 42ms"
    - error: "x tool_name(args) error message"
    """

    DEFAULT_CSS = """
    ToolCard {
        margin: 0 0 0 2;
        height: auto;
    }
    """

    def __init__(
        self,
        tool_name: str,
        args: dict | None = None,
        call_id: str = "",
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        self.tool_name = tool_name
        self.tool_args = args or {}
        self.call_id = call_id
        self._state = "running"
        self._duration_ms: int | None = None
        self._error: str | None = None
        super().__init__(
            Content.from_markup(self._format()),
            name=name,
            id=id,
            classes=classes,
        )

    def _format(self) -> str:
        args_str = ", ".join(f"{k}={v!r}" for k, v in self.tool_args.items())
        if len(args_str) > 80:
            args_str = args_str[:77] + "..."
        call_str = f"{self.tool_name}({args_str})"

        if self._state == "running":
            return f"[dim]  \u25b6 {call_str} [yellow]running...[/yellow][/dim]"
        elif self._state == "error":
            return f"[dim]  \u2717 {call_str} [red]{self._error}[/red][/dim]"
        else:
            return f"[dim]  \u2713 {call_str} [green]{self._duration_ms}ms[/green][/dim]"

    def mark_complete(self, duration_ms: int) -> None:
        """Mark the tool call as completed."""
        self._state = "complete"
        self._duration_ms = duration_ms
        self.update(Content.from_markup(self._format()))

    def mark_error(self, error: str) -> None:
        """Mark the tool call as failed."""
        self._state = "error"
        self._error = error
        self.update(Content.from_markup(self._format()))

    @property
    def state(self) -> str:
        return self._state


class StatusBar(Static):
    """Bottom status bar showing connection state, model, and token counts."""

    DEFAULT_CSS = """
    StatusBar {
        dock: bottom;
        height: 1;
        background: $surface;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        self._connected = False
        self._model: str | None = None
        self._input_tokens = 0
        self._output_tokens = 0
        self._agent_count = 0
        super().__init__(
            Content.from_markup(self._format()),
            name=name,
            id=id,
            classes=classes,
        )

    def _format(self) -> str:
        if self._connected:
            status = "[green]\u25cf[/green] connected"
        else:
            status = "[red]\u25cf[/red] disconnected"

        parts = [status]

        if self._agent_count:
            parts.append(f"{self._agent_count} agents")

        if self._model:
            model_short = self._model.split("/")[-1]
            parts.append(f"model: {model_short}")

        if self._input_tokens or self._output_tokens:
            parts.append(f"tokens: {self._input_tokens}\u2192{self._output_tokens}")

        return " | ".join(parts)

    def set_connected(self, connected: bool, agent_count: int = 0) -> None:
        self._connected = connected
        self._agent_count = agent_count
        self.update(Content.from_markup(self._format()))

    def set_model_info(
        self,
        model: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        if model:
            self._model = model
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self.update(Content.from_markup(self._format()))


class WelcomeBanner(Static):
    """Startup banner with agent count and commands hint."""

    DEFAULT_CSS = """
    WelcomeBanner {
        margin: 1 2;
        padding: 1 2;
        border: solid $accent;
        height: auto;
    }
    """

    def __init__(
        self,
        agent_count: int = 0,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        text = (
            "[bold]Robothor[/bold]\n\n"
            f"Connected to engine with {agent_count} agent{'s' if agent_count != 1 else ''}.\n"
            "Type a message to chat, or [dim]/help[/dim] for commands.\n"
            "Press [dim]Escape[/dim] to abort a response."
        )
        super().__init__(
            Content.from_markup(text),
            name=name,
            id=id,
            classes=classes,
        )
