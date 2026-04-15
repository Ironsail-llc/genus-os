"""Tests for audit enrichment — user_id as first-class column."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ─── log_event accepts and stores user_id ────────────────────────────


class TestLogEventUserId:
    """Verify log_event passes user_id through to the INSERT statement."""

    @patch("robothor.audit.logger._get_connection")
    @patch("robothor.audit.logger._release_connection")
    def test_log_event_includes_user_id(self, mock_release, mock_get_conn):
        """user_id should appear in the INSERT params tuple."""
        from robothor.audit.logger import log_event

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (1, MagicMock(isoformat=lambda: "2026-04-10T00:00:00"))
        mock_conn.cursor.return_value = mock_cur
        mock_get_conn.return_value = mock_conn

        result = log_event(
            "agent.tool_call",
            "web_fetch",
            actor="main",
            user_id="user-alice",
        )

        assert result is not None
        assert result["id"] == 1

        # Check the SQL includes user_id column
        sql = mock_cur.execute.call_args[0][0]
        assert "user_id" in sql

        # Check user_id value is in the params tuple
        params = mock_cur.execute.call_args[0][1]
        assert "user-alice" in params

    @patch("robothor.audit.logger._get_connection")
    @patch("robothor.audit.logger._release_connection")
    def test_log_event_user_id_defaults_empty(self, mock_release, mock_get_conn):
        """user_id should default to empty string when not provided."""
        from robothor.audit.logger import log_event

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (2, MagicMock(isoformat=lambda: "2026-04-10T00:00:00"))
        mock_conn.cursor.return_value = mock_cur
        mock_get_conn.return_value = mock_conn

        log_event("agent.tool_call", "web_fetch", actor="main")

        params = mock_cur.execute.call_args[0][1]
        # Last param should be user_id=""
        assert params[-1] == ""


# ─── _audit_tool_call passes user_id through ────────────────────────


class TestAuditToolCallUserId:
    """Verify _audit_tool_call forwards user_id to log_event."""

    @patch("robothor.audit.logger.log_event")
    def test_audit_tool_call_passes_user_id(self, mock_log_event):
        from robothor.engine.tools.dispatch import _audit_tool_call

        _audit_tool_call("web_fetch", "main", "default", user_id="user-bob")

        mock_log_event.assert_called_once()
        call_kwargs = mock_log_event.call_args
        assert (
            call_kwargs.kwargs.get("user_id") == "user-bob"
            or call_kwargs[1].get("user_id") == "user-bob"
        )

    @patch("robothor.audit.logger.log_event")
    def test_audit_tool_call_user_id_defaults_empty(self, mock_log_event):
        from robothor.engine.tools.dispatch import _audit_tool_call

        _audit_tool_call("web_fetch", "main", "default")

        mock_log_event.assert_called_once()
        call_kwargs = mock_log_event.call_args
        assert call_kwargs.kwargs.get("user_id") == "" or call_kwargs[1].get("user_id") == ""

    @patch("robothor.audit.logger.log_event")
    def test_audit_tool_call_with_error_passes_user_id(self, mock_log_event):
        from robothor.engine.tools.dispatch import _audit_tool_call

        _audit_tool_call(
            "web_fetch",
            "main",
            "default",
            user_id="user-carol",
            status="error",
            error="timeout",
        )

        mock_log_event.assert_called_once()
        kwargs = mock_log_event.call_args.kwargs
        assert kwargs["user_id"] == "user-carol"
        assert kwargs["status"] == "error"


# ─── query_log filters by user_id ───────────────────────────────────


class TestQueryLogUserId:
    """Verify query_log accepts and applies user_id filter."""

    @patch("robothor.audit.logger._get_connection")
    @patch("robothor.audit.logger._release_connection")
    def test_query_log_filters_by_user_id(self, mock_release, mock_get_conn):
        from robothor.audit.logger import query_log

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_get_conn.return_value = mock_conn

        query_log(user_id="user-dave")

        sql = mock_cur.execute.call_args[0][0]
        params = mock_cur.execute.call_args[0][1]

        assert "user_id = %s" in sql
        assert "user-dave" in params

    @patch("robothor.audit.logger._get_connection")
    @patch("robothor.audit.logger._release_connection")
    def test_query_log_no_user_id_filter_when_none(self, mock_release, mock_get_conn):
        from robothor.audit.logger import query_log

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_get_conn.return_value = mock_conn

        query_log()

        sql = mock_cur.execute.call_args[0][0]
        # user_id should appear in SELECT but NOT as a WHERE filter
        where_clause = sql.split("WHERE")[1].split("ORDER")[0]
        assert "user_id = %s" not in where_clause

    @patch("robothor.audit.logger._get_connection")
    @patch("robothor.audit.logger._release_connection")
    def test_query_log_returns_user_id_in_results(self, mock_release, mock_get_conn):
        """query_log results should include user_id field."""
        from robothor.audit.logger import query_log

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_ts = MagicMock()
        mock_ts.isoformat.return_value = "2026-04-10T00:00:00"
        mock_cur.fetchall.return_value = [
            (
                1,
                mock_ts,
                "agent.tool_call",
                "agent",
                "main",
                "web_fetch",
                None,
                None,
                None,
                "ok",
                None,
                "user-eve",
            ),
        ]
        mock_conn.cursor.return_value = mock_cur
        mock_get_conn.return_value = mock_conn

        results = query_log()

        assert len(results) == 1
        assert results[0]["user_id"] == "user-eve"


# ─── _execute_tool accepts user_id ──────────────────────────────────


class TestExecuteToolUserId:
    """Verify _execute_tool passes user_id to audit calls."""

    @pytest.mark.asyncio
    @patch("robothor.engine.tools.dispatch._audit_tool_call")
    @patch("robothor.engine.tools.dispatch._get_handlers")
    @patch("robothor.engine.tools.get_registry")
    async def test_execute_tool_passes_user_id(self, mock_registry, mock_handlers, mock_audit):
        from robothor.engine.tools.dispatch import _execute_tool

        mock_registry.return_value.get_adapter_route.return_value = None

        async def fake_handler(args, ctx):
            return {"ok": True}

        mock_handlers.return_value = {"test_tool": fake_handler}

        await _execute_tool(
            "test_tool",
            {},
            agent_id="main",
            tenant_id="default",
            user_id="user-frank",
        )

        mock_audit.assert_called_once()
        kwargs = mock_audit.call_args
        assert kwargs.kwargs.get("user_id") == "user-frank" or "user-frank" in kwargs.args
