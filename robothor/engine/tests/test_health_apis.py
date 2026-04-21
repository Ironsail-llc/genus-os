"""Tests for the new API endpoints in robothor/engine/health.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import APIRouter
from starlette.testclient import TestClient


def _make_app():
    """Create a health app with mocked dependencies."""
    mock_config = MagicMock()
    mock_config.tenant_id = "test-tenant"
    mock_config.bot_token = ""
    mock_config.port = 18800

    from robothor.engine.health import create_health_app

    with (
        patch("robothor.engine.dashboards.get_dashboard_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_public_router", return_value=APIRouter()),
        patch("robothor.engine.webhooks.get_webhook_router", return_value=APIRouter()),
        patch("robothor.db.connection.get_connection"),
    ):
        app = create_health_app(mock_config, runner=None, workflow_engine=None)

    return app


@pytest.fixture(scope="module")
def client():
    """Create a TestClient for the health app."""
    app = _make_app()
    return TestClient(app, raise_server_exceptions=False)


class TestBuddyStatsEndpoint:
    """Test GET /api/buddy/stats."""

    def test_buddy_stats_endpoint(self, client: TestClient) -> None:
        """Mock BuddyEngine methods, verify response shape."""
        from datetime import date

        from robothor.engine.buddy import AgentScore, FleetStatus

        mock_fleet = FleetStatus(
            stat_date=date(2026, 4, 18),
            fleet_achievement_score=72,
            tasks_completed=10,
            per_agent=[
                AgentScore(
                    agent_id="email-responder",
                    achievement_score=84,
                    rating=4,
                    satisfied_goals=3,
                    breached_goals=1,
                    stat_date=date(2026, 4, 18),
                    rank=1,
                ),
                AgentScore(
                    agent_id="chat-monitor",
                    achievement_score=60,
                    rating=3,
                    satisfied_goals=2,
                    breached_goals=2,
                    stat_date=date(2026, 4, 18),
                    rank=2,
                ),
            ],
        )

        with (
            patch("robothor.engine.buddy.BuddyEngine.compute_daily_stats", return_value=mock_fleet),
            patch("robothor.engine.buddy.BuddyEngine.get_streak", return_value=(5, 12)),
        ):
            resp = client.get("/api/buddy/stats")

        assert resp.status_code == 200
        data = resp.json()
        assert data["stat_date"] == "2026-04-18"
        assert data["fleet_achievement_score"] == 72
        assert data["streak"]["current"] == 5
        assert data["streak"]["longest"] == 12
        assert data["today"]["tasks"] == 10
        assert len(data["agents"]) == 2
        assert data["agents"][0]["agent_id"] == "email-responder"
        assert data["agents"][0]["achievement_score"] == 84
        assert data["agents"][0]["satisfied_goals"] == 3


class TestBuddyLoopHealthEndpoint:
    """GET /api/buddy/loop-health — fleet-level view of the self-improve loop.

    Derived entirely from `crm_tasks` tags + timestamps (no new table).
    Surfaces four things the operator needs to decide if the loop is working:
    open-breach trend, finding→verified latency, escalation distribution,
    and rolling hold-rate.
    """

    def test_returns_expected_shape(self, client: TestClient) -> None:
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        # A mix of task states that exercises every KPI field.
        tasks = [
            # Open breach — unresolved, counts in open_breach_count_by_day
            {
                "id": "t-open-1",
                "status": "TODO",
                "tags": ["nightwatch", "self-improve", "main", "error_rate"],
                "created_at": now - timedelta(days=2),
                "updated_at": now - timedelta(days=2),
                "body": "",
            },
            {
                "id": "t-open-2",
                "status": "IN_PROGRESS",
                "tags": ["nightwatch", "self-improve", "main", "error_rate"],
                "created_at": now - timedelta(days=1),
                "updated_at": now - timedelta(days=1),
                "body": "",
            },
            # Escalation distribution samples
            {
                "id": "t-esc-1",
                "status": "IN_PROGRESS",
                "tags": ["self-improve", "escalation:1"],
                "created_at": now - timedelta(days=4),
                "updated_at": now - timedelta(days=4),
                "body": "",
            },
            {
                "id": "t-esc-2",
                "status": "IN_PROGRESS",
                "tags": ["self-improve", "escalation:2"],
                "created_at": now - timedelta(days=4),
                "updated_at": now - timedelta(days=4),
                "body": "",
            },
            # Verified resolved — contributes to latency + held stats
            {
                "id": "t-verif-1",
                "status": "DONE",
                "tags": [
                    "self-improve",
                    "verified_resolved",
                    f"verified_at:{(now - timedelta(days=8)).isoformat()}",
                    "held_7d=true",
                ],
                "created_at": now - timedelta(days=10),
                "updated_at": now - timedelta(days=8),
                "body": "",
            },
            {
                "id": "t-verif-2",
                "status": "DONE",
                "tags": [
                    "self-improve",
                    "verified_resolved",
                    f"verified_at:{(now - timedelta(days=9)).isoformat()}",
                    "held_7d=false",
                ],
                "created_at": now - timedelta(days=12),
                "updated_at": now - timedelta(days=9),
                "body": "",
            },
            # requires_human
            {
                "id": "t-human",
                "status": "IN_PROGRESS",
                "tags": ["self-improve", "escalation:3"],
                "created_at": now - timedelta(days=5),
                "updated_at": now - timedelta(days=5),
                "body": "",
                "requires_human": True,
            },
        ]

        with patch("robothor.crm.dal.list_tasks", return_value=tasks):
            resp = client.get("/api/buddy/loop-health")

        assert resp.status_code == 200
        data = resp.json()
        # Required top-level keys
        assert "open_breach_count_by_day" in data
        assert "time_to_verified_resolved_ms" in data
        assert "escalation_distribution" in data
        assert "held_7d_rate_rolling_14d" in data

        # Open-breach count is a list of {day, count} over 30d
        obd = data["open_breach_count_by_day"]
        assert isinstance(obd, list)
        assert len(obd) > 0
        assert all("day" in entry and "count" in entry for entry in obd)

        # Latency metrics populated from the two verified tasks
        lat = data["time_to_verified_resolved_ms"]
        assert lat["p50_ms"] is not None
        assert lat["p95_ms"] is not None
        assert lat["sample_size"] == 2

        # Escalation buckets: seen at least 1/2/requires_human
        esc = data["escalation_distribution"]
        assert esc["1"] >= 1
        assert esc["2"] >= 1
        assert esc["requires_human"] >= 1

        # Hold rate: 1 held=true / 2 scored = 0.5
        hold = data["held_7d_rate_rolling_14d"]
        assert hold["held_true"] == 1
        assert hold["held_false"] == 1
        assert hold["rate"] == pytest.approx(0.5)


class TestBuddyHistoryEndpoint:
    """Test GET /api/buddy/history."""

    def test_buddy_history_endpoint(self, client: TestClient) -> None:
        """Mock get_connection, verify response returns days array."""
        mock_rows = [
            ("2026-04-18", 10, 72, 5),
            ("2026-04-17", 8, 68, 4),
        ]

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = mock_rows
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch("robothor.db.connection.get_connection", return_value=mock_conn):
            resp = client.get("/api/buddy/history?days=7")

        assert resp.status_code == 200
        data = resp.json()
        assert "days" in data
        assert len(data["days"]) == 2
        assert data["days"][0]["tasks"] == 10
        assert data["days"][0]["achievement_score"] == 72
        assert data["days"][0]["streak"] == 5


class TestKairosDreamsEndpoint:
    """Test GET /api/kairos/dreams."""

    def test_kairos_dreams_endpoint(self, client: TestClient) -> None:
        """Mock get_connection, verify response returns dreams array."""
        import uuid

        dream_id = str(uuid.uuid4())
        mock_rows = [
            (
                dream_id,
                "deep",
                "2026-04-03T02:00:00",
                "2026-04-03T02:05:00",
                300000,
                5,
                3,
                2,
                None,
            ),
        ]

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = mock_rows
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch("robothor.db.connection.get_connection", return_value=mock_conn):
            resp = client.get("/api/kairos/dreams?limit=5")

        assert resp.status_code == 200
        data = resp.json()
        assert "dreams" in data
        assert len(data["dreams"]) == 1
        dream = data["dreams"][0]
        assert dream["id"] == dream_id
        assert dream["mode"] == "deep"
        assert dream["duration_ms"] == 300000
        assert dream["facts_consolidated"] == 5
        assert dream["facts_pruned"] == 3
        assert dream["insights_discovered"] == 2
        assert dream["error"] is None


class TestExtensionsEndpoint:
    """Test GET /api/extensions."""

    def test_extensions_endpoint(self, client: TestClient) -> None:
        """Mock get_loaded_adapters, verify response shape."""
        mock_adapter = MagicMock()
        mock_adapter.name = "test-adapter"
        mock_adapter.transport = "http"
        mock_adapter.version = "1.0.0"
        mock_adapter.author = "tester"
        mock_adapter.description = "A test adapter"
        mock_adapter.agents = ["main"]

        with patch("robothor.engine.adapters.get_loaded_adapters", return_value=[mock_adapter]):
            resp = client.get("/api/extensions")

        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert len(data["extensions"]) == 1
        ext = data["extensions"][0]
        assert ext["name"] == "test-adapter"
        assert ext["transport"] == "http"
        assert ext["version"] == "1.0.0"
        assert ext["agents"] == ["main"]


class TestExtensionsReloadEndpoint:
    """Test POST /api/extensions/reload."""

    def test_extensions_reload_endpoint(self, client: TestClient) -> None:
        """Mock refresh_adapters, verify reloaded=True."""
        mock_adapter = MagicMock()
        mock_adapter.name = "reloaded"

        with patch("robothor.engine.adapters.refresh_adapters", return_value=[mock_adapter]):
            resp = client.post("/api/extensions/reload")

        assert resp.status_code == 200
        data = resp.json()
        assert data["reloaded"] is True
        assert data["count"] == 1
