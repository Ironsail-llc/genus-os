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

        from robothor.engine.buddy import DailyStats, LevelInfo

        mock_stats = DailyStats(
            stat_date=date(2026, 4, 3),
            tasks_completed=10,
            emails_processed=5,
            insights_generated=3,
            errors_avoided=1,
            dreams_completed=2,
            debugging_score=70,
            patience_score=60,
            chaos_score=40,
            wisdom_score=80,
            reliability_score=90,
        )
        mock_level = LevelInfo(
            level=7,
            total_xp=2800,
            xp_for_current_level=700,
            xp_for_next_level=800,
            progress_pct=0.35,
        )

        with (
            patch("robothor.engine.buddy.BuddyEngine.compute_daily_stats", return_value=mock_stats),
            patch("robothor.engine.buddy.BuddyEngine.get_level_info", return_value=mock_level),
            patch("robothor.engine.buddy.BuddyEngine.get_streak", return_value=(5, 12)),
        ):
            resp = client.get("/api/buddy/stats")

        assert resp.status_code == 200
        data = resp.json()
        assert data["level"] == 7
        assert data["level_name"] == "Flame"
        assert data["total_xp"] == 2800
        assert data["streak"]["current"] == 5
        assert data["streak"]["longest"] == 12
        assert data["today"]["tasks"] == 10
        assert data["today"]["emails"] == 5
        assert data["scores"]["debugging"] == 70
        assert data["scores"]["wisdom"] == 80


class TestBuddyHistoryEndpoint:
    """Test GET /api/buddy/history."""

    def test_buddy_history_endpoint(self, client: TestClient) -> None:
        """Mock get_connection, verify response returns days array."""
        mock_rows = [
            ("2026-04-03", 10, 2800, 7, 5, 70, 60, 40, 80, 90),
            ("2026-04-02", 8, 2500, 6, 4, 65, 55, 35, 75, 85),
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
        assert data["days"][0]["xp"] == 2800
        assert data["days"][0]["scores"]["debugging"] == 70


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
