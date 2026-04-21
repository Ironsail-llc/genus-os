"""Tests for the analytics module — cross-agent performance analysis."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch


def _mock_cursor(rows_sequence):
    """Create a mock cursor that returns different result sets per execute() call.

    rows_sequence: list of lists — each inner list is the rows for one query.
    """
    cursor = MagicMock()
    call_idx = [0]

    def side_effect_execute(*args, **kwargs):
        pass

    def side_effect_fetchone():
        idx = call_idx[0]
        call_idx[0] += 1
        rows = rows_sequence[idx] if idx < len(rows_sequence) else []
        return rows[0] if rows else None

    def side_effect_fetchall():
        idx = call_idx[0]
        call_idx[0] += 1
        return rows_sequence[idx] if idx < len(rows_sequence) else []

    cursor.execute = MagicMock(side_effect=side_effect_execute)
    cursor.fetchone = side_effect_fetchone
    cursor.fetchall = side_effect_fetchall
    return cursor


def _make_conn(cursor):
    """Create a mock connection context manager that returns the given cursor."""
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


class TestGetAgentStats:
    @patch("robothor.engine.analytics.get_connection")
    def test_basic_stats(self, mock_get_conn):
        cursor = _mock_cursor(
            [
                # First query: aggregate stats
                [
                    {
                        "total_runs": 10,
                        "completed": 8,
                        "failed": 2,
                        "timeouts": 0,
                        "budget_exhausted": 0,
                        "avg_duration_ms": Decimal("5000.0"),
                        "avg_tokens": Decimal("1500.0"),
                        "avg_cost_usd": Decimal("0.05"),
                        "total_cost_usd": Decimal("0.40"),
                        "total_input_tokens": 10000,
                        "total_output_tokens": 5000,
                    }
                ],
                # Second query: top error types
                [
                    {"error_type": "timeout", "count": 1},
                    {"error_type": "other", "count": 1},
                ],
            ]
        )
        mock_get_conn.return_value = _make_conn(cursor)

        from robothor.engine.analytics import get_agent_stats

        result = get_agent_stats("email-classifier", days=7)

        assert result["total_runs"] == 10
        assert result["success_rate"] == 0.8
        assert result["error_rate"] == 0.2
        assert result["avg_cost_usd"] == 0.05
        assert len(result["top_error_types"]) == 2

    @patch("robothor.engine.analytics.get_connection")
    def test_tool_success_rate_computed(self, mock_get_conn):
        """tool_success_rate = tool_ok / tool_calls. Adds queries 8+."""
        cursor = _mock_cursor(
            [
                # 1. aggregate
                [
                    {
                        "total_runs": 5,
                        "completed": 5,
                        "failed": 0,
                        "timeouts": 0,
                        "budget_exhausted": 0,
                        "avg_duration_ms": Decimal(1000),
                        "avg_tokens": None,
                        "avg_cost_usd": None,
                        "total_cost_usd": None,
                        "total_input_tokens": None,
                        "total_output_tokens": None,
                    }
                ],
                # 2. top error types
                [],
                # 3. outcome distribution
                [],
                # 4. percentiles
                [{"p95_duration_ms": Decimal(2000), "p95_cost_usd": None}],
                # 5. delivery success rate
                [{"announce_runs": 0, "delivered": 0}],
                # 6. median/min output chars
                [{"median_chars": Decimal(500), "avg_chars": Decimal(600)}],
                # 7. operator rating
                [{"avg_rating": None}],
                # 8. tool_success_rate — 19 ok out of 20 tool calls
                [{"tool_calls": 20, "tool_ok": 19}],
                # 9. recovery_rate — 2 error runs, 1 recovered
                [{"error_total": 2, "recovered_total": 1}],
            ]
        )
        mock_get_conn.return_value = _make_conn(cursor)

        from robothor.engine.analytics import get_agent_stats

        result = get_agent_stats("x", days=7)
        assert result["tool_success_rate"] == 0.95  # 19/20
        assert result["recovery_rate"] == 0.5  # 1/2

    @patch("robothor.engine.analytics.get_connection")
    def test_tool_success_rate_null_when_no_tool_calls(self, mock_get_conn):
        """Return None (not 0) when there were no tool calls — no penalty."""
        cursor = _mock_cursor(
            [
                [
                    {
                        "total_runs": 0,
                        "completed": 0,
                        "failed": 0,
                        "timeouts": 0,
                        "budget_exhausted": 0,
                        "avg_duration_ms": None,
                        "avg_tokens": None,
                        "avg_cost_usd": None,
                        "total_cost_usd": None,
                        "total_input_tokens": None,
                        "total_output_tokens": None,
                    }
                ],
                [],
                [],
                [{"p95_duration_ms": None, "p95_cost_usd": None}],
                [{"announce_runs": 0, "delivered": 0}],
                [{"median_chars": None, "avg_chars": None}],
                [{"avg_rating": None}],
                [{"tool_calls": 0, "tool_ok": 0}],
                [{"error_total": 0, "recovered_total": 0}],
            ]
        )
        mock_get_conn.return_value = _make_conn(cursor)

        from robothor.engine.analytics import get_agent_stats

        result = get_agent_stats("idle-agent", days=7)
        assert result["tool_success_rate"] is None
        assert result["recovery_rate"] is None  # null means "nothing to evaluate"

    @patch("robothor.engine.analytics.get_connection")
    def test_no_runs_returns_none_rates(self, mock_get_conn):
        cursor = _mock_cursor(
            [
                [
                    {
                        "total_runs": 0,
                        "completed": 0,
                        "failed": 0,
                        "timeouts": 0,
                        "budget_exhausted": 0,
                        "avg_duration_ms": None,
                        "avg_tokens": None,
                        "avg_cost_usd": None,
                        "total_cost_usd": None,
                        "total_input_tokens": None,
                        "total_output_tokens": None,
                    }
                ],
                [],
            ]
        )
        mock_get_conn.return_value = _make_conn(cursor)

        from robothor.engine.analytics import get_agent_stats

        result = get_agent_stats("nonexistent", days=7)
        assert result["success_rate"] is None
        assert result["error_rate"] is None


class TestGetFleetHealth:
    @patch("robothor.engine.analytics.get_connection")
    def test_fleet_summary(self, mock_get_conn):
        cursor = _mock_cursor(
            [
                [
                    {
                        "agent_id": "email-classifier",
                        "total_runs": 10,
                        "completed": 9,
                        "failed": 1,
                        "timeouts": 0,
                        "avg_cost_usd": Decimal("0.05"),
                        "total_cost_usd": Decimal("0.45"),
                        "last_run_at": "2026-03-04 10:00:00",
                    },
                    {
                        "agent_id": "vision-monitor",
                        "total_runs": 5,
                        "completed": 5,
                        "failed": 0,
                        "timeouts": 0,
                        "avg_cost_usd": Decimal("0.02"),
                        "total_cost_usd": Decimal("0.10"),
                        "last_run_at": "2026-03-04 09:00:00",
                    },
                ],
            ]
        )
        mock_get_conn.return_value = _make_conn(cursor)

        from robothor.engine.analytics import get_fleet_health

        result = get_fleet_health(days=1)

        assert len(result["agents"]) == 2
        assert result["agents"][0]["agent_id"] == "email-classifier"
        assert result["agents"][0]["success_rate"] == 0.9
        assert result["fleet_totals"]["total_runs"] == 15
        assert result["fleet_totals"]["completed"] == 14
        assert result["fleet_totals"]["total_cost_usd"] == 0.55

    @patch("robothor.engine.analytics.get_connection")
    def test_empty_fleet(self, mock_get_conn):
        cursor = _mock_cursor([[]])
        mock_get_conn.return_value = _make_conn(cursor)

        from robothor.engine.analytics import get_fleet_health

        result = get_fleet_health(days=1)
        assert result["agents"] == []
        assert result["fleet_totals"]["total_runs"] == 0
        assert result["fleet_totals"]["success_rate"] is None


class TestDetectAnomalies:
    @patch("robothor.engine.analytics.get_connection")
    def test_detects_high_error_rate(self, mock_get_conn):
        # Baseline: 7 days of low error rates
        baseline_rows = [
            {
                "day": f"2026-02-{25 + i}",
                "total_runs": 10,
                "completed": 9,
                "failed": 1,
                "avg_duration_ms": Decimal(5000),
                "avg_cost_usd": Decimal("0.05"),
                "avg_tokens": Decimal(1500),
            }
            for i in range(5)
        ]
        # Recent: sudden spike in errors
        recent_row = {
            "total_runs": 10,
            "completed": 3,
            "failed": 7,
            "avg_duration_ms": Decimal(5000),
            "avg_cost_usd": Decimal("0.05"),
            "avg_tokens": Decimal(1500),
        }

        cursor = _mock_cursor([baseline_rows, [recent_row]])
        mock_get_conn.return_value = _make_conn(cursor)

        from robothor.engine.analytics import detect_anomalies

        result = detect_anomalies("email-classifier")

        assert result["agent_id"] == "email-classifier"
        assert len(result["anomalies"]) >= 1
        error_anomaly = next((a for a in result["anomalies"] if a["metric"] == "error_rate"), None)
        assert error_anomaly is not None
        assert error_anomaly["direction"] == "higher"

    @patch("robothor.engine.analytics.get_connection")
    def test_no_anomalies_when_normal(self, mock_get_conn):
        baseline_rows = [
            {
                "day": f"2026-02-{25 + i}",
                "total_runs": 10,
                "completed": 9,
                "failed": 1,
                "avg_duration_ms": Decimal(5000),
                "avg_cost_usd": Decimal("0.05"),
                "avg_tokens": Decimal(1500),
            }
            for i in range(5)
        ]
        # Recent: same as baseline
        recent_row = {
            "total_runs": 10,
            "completed": 9,
            "failed": 1,
            "avg_duration_ms": Decimal(5000),
            "avg_cost_usd": Decimal("0.05"),
            "avg_tokens": Decimal(1500),
        }

        cursor = _mock_cursor([baseline_rows, [recent_row]])
        mock_get_conn.return_value = _make_conn(cursor)

        from robothor.engine.analytics import detect_anomalies

        result = detect_anomalies("email-classifier")
        assert result["anomalies"] == []

    @patch("robothor.engine.analytics.get_connection")
    def test_insufficient_baseline(self, mock_get_conn):
        cursor = _mock_cursor(
            [
                [],
                [
                    {
                        "total_runs": 5,
                        "completed": 4,
                        "failed": 1,
                        "avg_duration_ms": None,
                        "avg_cost_usd": None,
                        "avg_tokens": None,
                    }
                ],
            ]
        )
        mock_get_conn.return_value = _make_conn(cursor)

        from robothor.engine.analytics import detect_anomalies

        result = detect_anomalies("new-agent")
        assert result["anomalies"] == []

    @patch("robothor.engine.analytics.get_connection")
    def test_no_recent_runs(self, mock_get_conn):
        cursor = _mock_cursor(
            [
                [
                    {
                        "day": "2026-02-25",
                        "total_runs": 10,
                        "completed": 9,
                        "failed": 1,
                        "avg_duration_ms": Decimal(5000),
                        "avg_cost_usd": Decimal("0.05"),
                        "avg_tokens": Decimal(1500),
                    }
                ],
                [
                    {
                        "total_runs": 0,
                        "completed": 0,
                        "failed": 0,
                        "avg_duration_ms": None,
                        "avg_cost_usd": None,
                        "avg_tokens": None,
                    }
                ],
            ]
        )
        mock_get_conn.return_value = _make_conn(cursor)

        from robothor.engine.analytics import detect_anomalies

        result = detect_anomalies("idle-agent")
        assert result["anomalies"] == []


class TestGetFailurePatterns:
    @patch("robothor.engine.analytics.get_connection")
    def test_groups_failures(self, mock_get_conn):
        cursor = _mock_cursor(
            [
                [
                    {
                        "agent_id": "email-classifier",
                        "error_type": "timeout",
                        "count": 3,
                        "last_occurrence": "2026-03-04 10:00:00",
                        "sample_messages": ["timeout waiting for response"],
                    },
                    {
                        "agent_id": "vision-monitor",
                        "error_type": "connection_error",
                        "count": 2,
                        "last_occurrence": "2026-03-04 09:00:00",
                        "sample_messages": ["connection refused", "connection reset"],
                    },
                ],
            ]
        )
        mock_get_conn.return_value = _make_conn(cursor)

        from robothor.engine.analytics import get_failure_patterns

        result = get_failure_patterns(hours=24)

        assert result["total_clusters"] == 2
        assert result["patterns"][0]["agent_id"] == "email-classifier"
        assert result["patterns"][0]["count"] == 3
        assert result["period_hours"] == 24

    @patch("robothor.engine.analytics.get_connection")
    def test_no_failures(self, mock_get_conn):
        cursor = _mock_cursor([[]])
        mock_get_conn.return_value = _make_conn(cursor)

        from robothor.engine.analytics import get_failure_patterns

        result = get_failure_patterns(hours=24)
        assert result["total_clusters"] == 0
        assert result["patterns"] == []
