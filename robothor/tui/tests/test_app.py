"""Tests for the main RobothorApp — Textual pilot tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from robothor.tui.app import RobothorApp
from robothor.tui.widgets import MessageDisplay, WelcomeBanner


class TestAppInit:
    def test_default_session_key(self):
        """Default session key includes username."""
        import getpass

        app = RobothorApp()
        username = getpass.getuser()
        assert f"tui-{username}" in app.client.session_key

    def test_custom_session_key(self):
        """Custom session key is used when provided."""
        app = RobothorApp(session_key="custom:main:test")
        assert app.client.session_key == "custom:main:test"

    def test_custom_engine_url(self):
        """Custom engine URL is passed to client."""
        app = RobothorApp(engine_url="http://localhost:9999")
        assert "localhost:9999" in app.client.base_url


class TestAppPilot:
    @pytest.mark.asyncio
    async def test_connect_error_shows_message(self):
        """When engine is unreachable, shows error message."""
        app = RobothorApp()
        # Patch the client's health check to return None
        app.client.check_health = AsyncMock(return_value=None)
        app.client.get_history = AsyncMock(return_value=[])
        app.client.close = AsyncMock()

        async with app.run_test() as _pilot:
            status_bar = app.query_one("#status-bar")
            assert "disconnected" in status_bar._format()

    @pytest.mark.asyncio
    async def test_connected_shows_welcome(self):
        """When engine is reachable, shows welcome banner."""
        app = RobothorApp()
        app.client.check_health = AsyncMock(
            return_value={
                "status": "healthy",
                "agents": {"a": {}, "b": {}},
            }
        )
        app.client.get_history = AsyncMock(return_value=[])
        app.client.close = AsyncMock()

        async with app.run_test() as _pilot:
            banners = app.query(WelcomeBanner)
            assert len(banners) > 0

    @pytest.mark.asyncio
    async def test_slash_command_handled_locally(self):
        """Slash commands are handled without sending to engine."""
        app = RobothorApp()
        app.client.check_health = AsyncMock(
            return_value={
                "status": "healthy",
                "agents": {},
            }
        )
        app.client.get_history = AsyncMock(return_value=[])
        app.client.close = AsyncMock()
        app.client.send_message = AsyncMock()

        async with app.run_test() as pilot:
            input_widget = app.query_one("#message-input")
            input_widget.value = "/help"
            await pilot.press("enter")

            # Give the UI a moment to process
            await pilot.pause()

            # Engine's send_message should not have been called
            app.client.send_message.assert_not_called()

            # System message should appear in chat
            messages = app.query(MessageDisplay)
            system_msgs = [m for m in messages if m._role == "system"]
            assert len(system_msgs) >= 1
