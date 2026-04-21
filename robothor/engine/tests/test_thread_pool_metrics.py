"""Stage 4 — thread pool observability metrics."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestThreadPoolMetrics:
    def test_returns_expected_keys(self):
        from robothor.engine.analytics import thread_pool_metrics

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = None
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch("robothor.engine.analytics.get_connection", return_value=mock_conn):
            out = thread_pool_metrics(tenant_id="default", window_days=7)

        for k in [
            "threads_advanced_per_beat",
            "next_action_source",
            "questions_answered_within_24h",
            "stall_rate",
            "planner_override_rate",
            "window_days",
        ]:
            assert k in out, f"missing key {k}"
