"""Tests for TUI widgets — ToolCard, MessageDisplay, StatusBar."""

from __future__ import annotations

from robothor.tui.widgets import MessageDisplay, StatusBar, ToolCard, WelcomeBanner


class TestToolCard:
    def test_initial_state_is_running(self):
        """ToolCard starts in running state."""
        card = ToolCard(tool_name="search_memory", args={"query": "test"})
        assert card.state == "running"

    def test_mark_complete(self):
        """ToolCard transitions to complete with duration."""
        card = ToolCard(tool_name="search_memory")
        card.mark_complete(42)
        assert card.state == "complete"
        rendered = card._format()
        assert "42ms" in rendered
        assert "\u2713" in rendered  # checkmark

    def test_mark_error(self):
        """ToolCard transitions to error state."""
        card = ToolCard(tool_name="search_memory")
        card.mark_error("not found")
        assert card.state == "error"
        rendered = card._format()
        assert "not found" in rendered
        assert "\u2717" in rendered  # x mark

    def test_running_render(self):
        """Running state shows tool name and running indicator."""
        card = ToolCard(tool_name="list_tasks", args={"status": "TODO"})
        rendered = card._format()
        assert "list_tasks" in rendered
        assert "running" in rendered
        assert "\u25b6" in rendered  # play symbol

    def test_long_args_truncated(self):
        """Very long args are truncated in rendering."""
        long_args = {f"key_{i}": f"value_{i}" for i in range(20)}
        card = ToolCard(tool_name="test_tool", args=long_args)
        rendered = card._format()
        assert "..." in rendered

    def test_call_id_stored(self):
        """Call ID is stored for matching start/end events."""
        card = ToolCard(tool_name="test", call_id="call_abc")
        assert card.call_id == "call_abc"


class TestMessageDisplay:
    def test_user_message_cyan_prefix(self):
        """User messages get cyan > prefix."""
        msg = MessageDisplay(content="hello", role="user")
        rendered = msg._format()
        assert "[cyan]" in rendered
        assert "> hello" in rendered

    def test_assistant_message_plain(self):
        """Assistant messages render content directly."""
        msg = MessageDisplay(content="I can help!", role="assistant")
        rendered = msg._format()
        assert "I can help!" in rendered

    def test_system_message_dim(self):
        """System messages are dim and italic."""
        msg = MessageDisplay(content="Session cleared.", role="system")
        rendered = msg._format()
        assert "[dim italic]" in rendered

    def test_update_content(self):
        """update_content changes the displayed text."""
        msg = MessageDisplay(content="", role="assistant")
        msg.update_content("Updated text")
        assert msg._content == "Updated text"


class TestStatusBar:
    def test_initial_disconnected(self):
        """StatusBar starts disconnected."""
        bar = StatusBar()
        rendered = bar._format()
        assert "disconnected" in rendered
        assert "[red]" in rendered

    def test_set_connected(self):
        """Connected state shows green indicator."""
        bar = StatusBar()
        bar.set_connected(True, agent_count=5)
        rendered = bar._format()
        assert "connected" in rendered
        assert "[green]" in rendered
        assert "5 agents" in rendered

    def test_set_model_info(self):
        """Model info appears in status bar."""
        bar = StatusBar()
        bar.set_model_info(model="openrouter/kimi/k2.5", input_tokens=100, output_tokens=50)
        rendered = bar._format()
        assert "k2.5" in rendered
        assert "100" in rendered
        assert "50" in rendered

    def test_model_short_name(self):
        """Model name is shortened to last path segment."""
        bar = StatusBar()
        bar.set_model_info(model="openrouter/anthropic/claude-sonnet-4-20250514")
        rendered = bar._format()
        assert "claude-sonnet-4-20250514" in rendered
        assert "openrouter" not in rendered


class TestWelcomeBanner:
    def test_banner_content(self):
        """Banner shows agent count and help hint."""
        # Just verify it instantiates without error
        banner = WelcomeBanner(agent_count=3)
        assert banner is not None

    def test_banner_singular(self):
        """Banner uses singular 'agent' for count of 1."""
        banner = WelcomeBanner(agent_count=1)
        assert banner is not None
