"""Tests for DevOps metrics storage tool handlers."""

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


class TestDevopsMetricsSchemas:
    def test_tools_registered(self):
        with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
            from robothor.engine.tools import ToolRegistry

            registry = ToolRegistry()
            assert "devops_store_metric" in registry._schemas
            assert "devops_query_metrics" in registry._schemas

    def test_query_in_readonly(self):
        from robothor.engine.tools import READONLY_TOOLS

        assert "devops_query_metrics" in READONLY_TOOLS
        assert "devops_store_metric" not in READONLY_TOOLS

    def test_tools_in_set(self):
        from robothor.engine.tools import DEVOPS_METRICS_TOOLS

        assert len(DEVOPS_METRICS_TOOLS) == 2


class TestStoreMetric:
    @pytest.mark.asyncio
    async def test_missing_required_fields(self):
        from robothor.engine.tools.handlers.devops_metrics import _devops_store_metric

        result = await _devops_store_metric({}, _CTX)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_missing_value(self):
        from robothor.engine.tools.handlers.devops_metrics import _devops_store_metric

        result = await _devops_store_metric({"source": "jira", "metric_type": "velocity"}, _CTX)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_store_success(self):
        from robothor.engine.tools.handlers.devops_metrics import _devops_store_metric

        mock_cursor = MagicMock()
        fake_conn, mock_conn = _mock_db(mock_cursor)

        with patch("robothor.engine.tools.handlers.devops_metrics._get_conn", fake_conn):
            result = await _devops_store_metric(
                {"source": "jira", "metric_type": "sprint_velocity", "value": {"committed": 30}},
                _CTX,
            )
        assert result["stored"] is True
        mock_cursor.execute.assert_called_once()
        mock_conn.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_store_db_error(self):
        from robothor.engine.tools.handlers.devops_metrics import _devops_store_metric

        @contextmanager
        def _fail():
            raise Exception("Connection refused")
            yield  # noqa: RET503

        with patch("robothor.engine.tools.handlers.devops_metrics._get_conn", _fail):
            result = await _devops_store_metric(
                {"source": "github", "metric_type": "pr_cycle_time", "value": 24.5}, _CTX
            )
        assert "error" in result


class TestQueryMetrics:
    @pytest.mark.asyncio
    async def test_missing_required_fields(self):
        from robothor.engine.tools.handlers.devops_metrics import _devops_query_metrics

        result = await _devops_query_metrics({}, _CTX)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_query_success(self):
        from datetime import UTC, date, datetime

        from robothor.engine.tools.handlers.devops_metrics import _devops_query_metrics

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            (
                date(2026, 4, 7),
                "team",
                "ENG",
                {"committed": 30},
                datetime(2026, 4, 7, 10, 0, tzinfo=UTC),
            ),
            (
                date(2026, 3, 31),
                "team",
                "ENG",
                {"committed": 28},
                datetime(2026, 3, 31, 10, 0, tzinfo=UTC),
            ),
        ]
        fake_conn, _ = _mock_db(mock_cursor)

        with patch("robothor.engine.tools.handlers.devops_metrics._get_conn", fake_conn):
            result = await _devops_query_metrics(
                {"source": "jira", "metric_type": "sprint_velocity", "days": 30}, _CTX
            )
        assert result["count"] == 2
        assert result["snapshots"][0]["scope_key"] == "ENG"

    @pytest.mark.asyncio
    async def test_query_with_scope_filter(self):
        from robothor.engine.tools.handlers.devops_metrics import _devops_query_metrics

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        fake_conn, _ = _mock_db(mock_cursor)

        with patch("robothor.engine.tools.handlers.devops_metrics._get_conn", fake_conn):
            result = await _devops_query_metrics(
                {
                    "source": "github",
                    "metric_type": "pr_cycle_time",
                    "scope": "repo",
                    "scope_key": "acme/test-repo",
                },
                _CTX,
            )
        assert result["count"] == 0
        query = mock_cursor.execute.call_args[0][0]
        assert "scope = %s" in query

    @pytest.mark.asyncio
    async def test_query_db_error(self):
        from robothor.engine.tools.handlers.devops_metrics import _devops_query_metrics

        @contextmanager
        def _fail():
            raise Exception("Connection refused")
            yield  # noqa: RET503

        with patch("robothor.engine.tools.handlers.devops_metrics._get_conn", _fail):
            result = await _devops_query_metrics(
                {"source": "jira", "metric_type": "velocity"}, _CTX
            )
        assert "error" in result
