"""Tests for identity mapping tool handlers."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.tools.dispatch import ToolContext

_CTX = ToolContext(agent_id="test", tenant_id="test-tenant")


def _mock_db(cursor_mock):
    """Create a mock _get_conn that yields a connection with the given cursor."""
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    @contextmanager
    def _fake_conn():
        yield mock_conn

    return _fake_conn, mock_conn


class TestIdentityToolSchemas:
    def test_tools_registered(self):
        with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
            from robothor.engine.tools import ToolRegistry

            registry = ToolRegistry()
            assert "link_identity" in registry._schemas
            assert "resolve_identities" in registry._schemas

    def test_resolve_in_readonly(self):
        from robothor.engine.tools import READONLY_TOOLS

        assert "resolve_identities" in READONLY_TOOLS
        assert "link_identity" not in READONLY_TOOLS

    def test_tools_in_set(self):
        from robothor.engine.tools import IDENTITY_TOOLS

        assert len(IDENTITY_TOOLS) == 2


class TestLinkIdentity:
    @pytest.mark.asyncio
    async def test_missing_required_fields(self):
        from robothor.engine.tools.handlers.identity import _link_identity

        result = await _link_identity({}, _CTX)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_link_success(self):
        from robothor.engine.tools.handlers.identity import _link_identity

        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (42,)
        fake_conn, mock_conn = _mock_db(mock_cursor)

        with patch("robothor.engine.tools.handlers.identity._get_conn", fake_conn):
            result = await _link_identity(
                {
                    "person_id": "af1829a2-3ea2-4a59-9bbc-7e76a1b14d5a",
                    "channel": "github",
                    "identifier": "alice-dev",
                },
                _CTX,
            )
        assert result["linked"] is True
        mock_cursor.execute.assert_called_once()
        mock_conn.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_link_db_error(self):
        from robothor.engine.tools.handlers.identity import _link_identity

        @contextmanager
        def _fail():
            raise Exception("Connection refused")
            yield  # noqa: RET503

        with patch("robothor.engine.tools.handlers.identity._get_conn", _fail):
            result = await _link_identity(
                {"person_id": "abc", "channel": "github", "identifier": "test"}, _CTX
            )
        assert "error" in result


class TestResolveIdentities:
    @pytest.mark.asyncio
    async def test_missing_params(self):
        from robothor.engine.tools.handlers.identity import _resolve_identities

        result = await _resolve_identities({}, _CTX)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_resolve_by_person_id(self):
        from robothor.engine.tools.handlers.identity import _resolve_identities

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            ("github", "alice-dev", "Alice Dev"),
            ("jira_display_name", "Alice Developer", None),
        ]
        mock_cursor.fetchone.return_value = ("Alice", "Dev", "alice@example.com")
        fake_conn, _ = _mock_db(mock_cursor)

        with patch("robothor.engine.tools.handlers.identity._get_conn", fake_conn):
            result = await _resolve_identities(
                {"person_id": "af1829a2-3ea2-4a59-9bbc-7e76a1b14d5a"}, _CTX
            )
        assert result["count"] == 2
        assert result["name"] == "Alice Dev"

    @pytest.mark.asyncio
    async def test_resolve_by_channel(self):
        from robothor.engine.tools.handlers.identity import _resolve_identities

        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [
            ("af1829a2",),  # resolve lookup
            ("Alice", "Dev", "alice@example.com"),  # person details
        ]
        mock_cursor.fetchall.return_value = [("github", "alice-dev", "Alice")]
        fake_conn, _ = _mock_db(mock_cursor)

        with patch("robothor.engine.tools.handlers.identity._get_conn", fake_conn):
            result = await _resolve_identities(
                {"channel": "github", "identifier": "alice-dev"}, _CTX
            )
        assert result["person_id"] == "af1829a2"

    @pytest.mark.asyncio
    async def test_resolve_not_found(self):
        from robothor.engine.tools.handlers.identity import _resolve_identities

        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        fake_conn, _ = _mock_db(mock_cursor)

        with patch("robothor.engine.tools.handlers.identity._get_conn", fake_conn):
            result = await _resolve_identities(
                {"channel": "github", "identifier": "nonexistent"}, _CTX
            )
        assert "error" in result
