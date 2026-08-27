"""Smoke tests for GET /health/timeouts/last-24h.

The endpoint reads reap_category from agent_runs and returns per-category
counts. We mock the DB cursor since creating the full FastAPI app here
would be heavyweight for one route; we verify the endpoint function's
shape by calling the underlying handler after attaching a patched
get_connection.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(engine_config):
    from robothor.engine.health import create_health_app

    app = create_health_app(engine_config, runner=None)
    return TestClient(app)


def _mock_rows(rows):
    """Return a mocked get_connection context manager yielding those rows."""
    cm = MagicMock()
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = rows
    conn.cursor.return_value = cur
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = None
    return cm


def test_timeouts_endpoint_aggregates_by_category(app_client) -> None:
    rows = [
        ("post_tool_crash", 4, ["buddy", "main"]),
        ("daemon_restart", 3, ["calendar-monitor", "email-responder"]),
        ("no_steps", 2, ["auto-agent"]),
        (None, 1, ["main"]),  # legacy rows without reap_category set
    ]
    with patch("robothor.db.connection.get_connection", return_value=_mock_rows(rows)):
        resp = app_client.get("/health/timeouts/last-24h")
    assert resp.status_code == 200
    body = resp.json()
    assert body["window_hours"] == 24
    assert body["total"] == 10
    assert body["uncategorized"] == 1
    cats = {c["category"]: c for c in body["categories"]}
    assert cats["post_tool_crash"]["count"] == 4
    assert "buddy" in cats["post_tool_crash"]["example_agents"]
    assert cats["daemon_restart"]["count"] == 3


def test_timeouts_endpoint_empty(app_client) -> None:
    with patch("robothor.db.connection.get_connection", return_value=_mock_rows([])):
        resp = app_client.get("/health/timeouts/last-24h")
    assert resp.status_code == 200
    body = resp.json()
    assert body["categories"] == []
    assert body["total"] == 0
    assert body["uncategorized"] == 0


class TestInterruptedRunsStayVisible:
    """Moving external cancels off `timeout` must not empty the operator's panel.

    2026-08-27: external cancellation now writes RunStatus.CANCELLED so `resume`
    can find it and the timeout RATE stops counting deploys. But this endpoint
    filtered `status = 'timeout'`, so the same change would have made every
    interrupted run vanish from the 24h view — trading one honesty bug for a
    blind spot. The panel counts both; the timeout *rate* (analytics.py) is
    where the distinction belongs.
    """

    def test_the_query_counts_cancelled_runs_too(self):
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "health.py").read_text()
        block = src.split("timeouts_last_24h", 1)[-1]
        window = block[: block.find("GROUP BY reap_category")]
        assert "cancelled" in window, (
            "interrupted runs disappeared from the 24h panel when they stopped "
            "being filed as timeouts"
        )
