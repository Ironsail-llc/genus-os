"""Tests for the slash command handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.tui.commands import handle_command, COMMANDS


@pytest.fixture
def mock_app(mock_client):
    """A mock RobothorApp for command handlers."""
    app = MagicMock()
    app.client = mock_client
    app.status_bar = MagicMock()
    app.status_bar._model = "openrouter/test/model"

    # Mock query_one for /clear
    chat_scroll = MagicMock()
    chat_scroll.remove_children = AsyncMock()
    app.query_one.return_value = chat_scroll

    app.exit = MagicMock()
    return app


class TestHandleCommand:
    @pytest.mark.asyncio
    async def test_non_command_not_handled(self, mock_app):
        """Regular text is not treated as a command."""
        handled, output = await handle_command(mock_app, "hello world")
        assert handled is False
        assert output is None

    @pytest.mark.asyncio
    async def test_unknown_command(self, mock_app):
        """Unknown slash command returns error message."""
        handled, output = await handle_command(mock_app, "/unknown")
        assert handled is True
        assert "Unknown command" in output

    @pytest.mark.asyncio
    async def test_all_commands_registered(self):
        """All expected commands are in the registry."""
        expected = {"/status", "/agents", "/costs", "/history", "/model",
                    "/clear", "/abort", "/help", "/quit", "/exit"}
        assert set(COMMANDS.keys()) == expected


class TestStatusCommand:
    @pytest.mark.asyncio
    async def test_status_connected(self, mock_app):
        """Status shows engine info when connected."""
        handled, output = await handle_command(mock_app, "/status")
        assert handled is True
        assert "healthy" in output
        assert "0.1.0" in output

    @pytest.mark.asyncio
    async def test_status_disconnected(self, mock_app):
        """Status shows error when engine unreachable."""
        mock_app.client.check_health = AsyncMock(return_value=None)
        handled, output = await handle_command(mock_app, "/status")
        assert handled is True
        assert "unreachable" in output.lower()


class TestAgentsCommand:
    @pytest.mark.asyncio
    async def test_agents_list(self, mock_app):
        """Agents command shows agent table."""
        handled, output = await handle_command(mock_app, "/agents")
        assert handled is True
        assert "email-classifier" in output
        assert "supervisor" in output

    @pytest.mark.asyncio
    async def test_agents_disconnected(self, mock_app):
        """Agents shows error when engine unreachable."""
        mock_app.client.check_health = AsyncMock(return_value=None)
        handled, output = await handle_command(mock_app, "/agents")
        assert handled is True
        assert "unreachable" in output.lower()


class TestCostsCommand:
    @pytest.mark.asyncio
    async def test_costs_default(self, mock_app):
        """Costs with no args uses 24h."""
        handled, output = await handle_command(mock_app, "/costs")
        assert handled is True
        assert "24h" in output
        assert "10" in output  # total_runs

    @pytest.mark.asyncio
    async def test_costs_custom_hours(self, mock_app):
        """Costs with custom hours argument."""
        handled, output = await handle_command(mock_app, "/costs 48")
        assert handled is True
        assert "48h" in output


class TestHistoryCommand:
    @pytest.mark.asyncio
    async def test_history_empty(self, mock_app):
        """History shows count of 0 for empty session."""
        handled, output = await handle_command(mock_app, "/history")
        assert handled is True
        assert "0 messages" in output


class TestModelCommand:
    @pytest.mark.asyncio
    async def test_model_display(self, mock_app):
        """Model command shows current model."""
        handled, output = await handle_command(mock_app, "/model")
        assert handled is True
        assert "model" in output.lower()


class TestClearCommand:
    @pytest.mark.asyncio
    async def test_clear_success(self, mock_app):
        """Clear resets session and returns success."""
        handled, output = await handle_command(mock_app, "/clear")
        assert handled is True
        assert "cleared" in output.lower()


class TestAbortCommand:
    @pytest.mark.asyncio
    async def test_abort_success(self, mock_app):
        """Abort cancels running response."""
        handled, output = await handle_command(mock_app, "/abort")
        assert handled is True
        assert "aborted" in output.lower()


class TestHelpCommand:
    @pytest.mark.asyncio
    async def test_help_lists_commands(self, mock_app):
        """Help shows all available commands."""
        handled, output = await handle_command(mock_app, "/help")
        assert handled is True
        assert "/status" in output
        assert "/agents" in output
        assert "/costs" in output
        assert "/help" in output
        assert "/quit" in output


class TestQuitCommand:
    @pytest.mark.asyncio
    async def test_quit_calls_exit(self, mock_app):
        """Quit calls app.exit()."""
        handled, output = await handle_command(mock_app, "/quit")
        assert handled is True
        mock_app.exit.assert_called_once()

    @pytest.mark.asyncio
    async def test_exit_alias(self, mock_app):
        """Exit is an alias for quit."""
        handled, output = await handle_command(mock_app, "/exit")
        assert handled is True
        mock_app.exit.assert_called_once()
