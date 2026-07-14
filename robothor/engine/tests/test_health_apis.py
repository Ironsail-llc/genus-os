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


class TestControlRouteAuth:
    """Control mutations require a signed owner/admin Engine capability."""

    @staticmethod
    def _secure_client(monkeypatch):
        monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
        monkeypatch.setenv("ROBOTHOR_ENGINE_HOST", "0.0.0.0")
        monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "engine-test-signing-key-32-bytes-minimum")
        from robothor.auth.tokens import reset_signing_key_cache

        reset_signing_key_cache()
        return TestClient(_make_app(), raise_server_exceptions=False)

    @staticmethod
    def _token(role="owner", scopes=("engine:*",), audience="genus-bridge"):
        from robothor.auth.tokens import issue_access_token

        return issue_access_token(
            "user-1",
            "test-tenant",
            role,
            audience=audience,
            scopes=scopes,
        )

    def test_rejects_missing_signed_identity(self, monkeypatch):
        c = self._secure_client(monkeypatch)
        for path in ("steer", "interrupt", "resume"):
            r = c.post(f"/api/runs/run-x/{path}", json={"text": "hi"})
            assert r.status_code == 401, f"{path} allowed without identity"

    def test_rejects_chat_only_identity(self, monkeypatch):
        c = self._secure_client(monkeypatch)
        token = self._token(role="member", scopes=("engine:chat", "engine:read"))
        r = c.post(
            "/api/runs/run-x/steer",
            json={"text": "hi"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403

    def test_rejects_wrong_audience(self, monkeypatch):
        c = self._secure_client(monkeypatch)
        token = self._token(audience="some-other-service")
        r = c.post(
            "/api/runs/run-x/steer",
            json={"text": "hi"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 401

    def test_accepts_signed_control_identity(self, monkeypatch):
        c = self._secure_client(monkeypatch)
        token = self._token()
        r = c.post(
            "/api/runs/run-x/steer",
            json={"text": "hi"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200


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


class TestAutodreamRunsEndpoint:
    """Test GET /api/dreams (autoDream runs)."""

    def test_autodream_runs_endpoint(self, client: TestClient) -> None:
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
            resp = client.get("/api/dreams?limit=5")

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


class TestActiveRunsEndpoint:
    """Test GET /api/runs/active — live sessions from the in-process registry (PR 1)."""

    @pytest.fixture(autouse=True)
    def _clear_registry(self):
        from robothor.engine import session_registry

        for run_id in session_registry.active_run_ids():
            session_registry.unregister(run_id)
        yield
        for run_id in session_registry.active_run_ids():
            session_registry.unregister(run_id)

    def test_empty_when_no_active_runs(self, client: TestClient) -> None:
        resp = client.get("/api/runs/active")
        assert resp.status_code == 200
        assert resp.json() == {"runs": []}

    def test_registered_session_appears(self, client: TestClient) -> None:
        from robothor.engine import session_registry
        from robothor.engine.session import AgentSession

        session = AgentSession(agent_id="test-agent")
        session.start("system prompt", "hello", [])
        session.record_llm_call(model="test/model")
        session_registry.register(session)

        resp = client.get("/api/runs/active")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["runs"]) == 1
        run = data["runs"][0]
        assert run["run_id"] == session.run_id
        assert run["agent_id"] == "test-agent"
        assert run["started_at"] is not None
        assert run["iterations"] == 1

    def test_requires_signed_control_identity(self, monkeypatch) -> None:
        c = TestControlRouteAuth._secure_client(monkeypatch)
        resp = c.get("/api/runs/active")
        assert resp.status_code == 401

    def test_accepts_signed_control_identity(self, monkeypatch) -> None:
        c = TestControlRouteAuth._secure_client(monkeypatch)
        token = TestControlRouteAuth._token()
        resp = c.get(
            "/api/runs/active",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
