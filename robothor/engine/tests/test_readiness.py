"""Engine liveness/readiness contract tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import APIRouter
from starlette.testclient import TestClient

from robothor.engine.config import EngineConfig


def _client(config: EngineConfig) -> TestClient:
    from robothor.engine.health import create_health_app

    with (
        patch("robothor.engine.dashboards.get_dashboard_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_public_router", return_value=APIRouter()),
        patch("robothor.engine.webhooks.get_webhook_router", return_value=APIRouter()),
    ):
        return TestClient(create_health_app(config), raise_server_exceptions=False)


def test_live_never_checks_dependencies(tmp_path):
    response = _client(EngineConfig(workspace=tmp_path, manifest_dir=tmp_path)).get("/live")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ready_rejects_empty_production_fleet(tmp_path):
    config = EngineConfig(
        workspace=tmp_path,
        manifest_dir=tmp_path,
        allow_empty_fleet=False,
        required_agent_ids=("main",),
    )
    redis_client = MagicMock()
    redis_client.ping = AsyncMock(return_value=True)
    redis_client.aclose = AsyncMock()

    with (
        patch("robothor.db.connection.get_connection"),
        patch("robothor.engine.tracking.list_schedules", return_value=[]),
        patch("robothor.engine.config.load_all_manifests", return_value=[]),
        patch("redis.asyncio.Redis", return_value=redis_client),
    ):
        response = _client(config).get("/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["fleet"].startswith("error:")


def test_ready_accepts_required_agent(tmp_path):
    config = EngineConfig(
        workspace=tmp_path,
        manifest_dir=tmp_path,
        allow_empty_fleet=False,
        required_agent_ids=("main",),
    )
    redis_client = MagicMock()
    redis_client.ping = AsyncMock(return_value=True)
    redis_client.aclose = AsyncMock()

    with (
        patch("robothor.db.connection.get_connection"),
        patch("robothor.engine.tracking.list_schedules", return_value=[]),
        patch("robothor.engine.config.load_all_manifests", return_value=[{"id": "main"}]),
        patch("robothor.federation.connections.load_connections", return_value=[]),
        patch("redis.asyncio.Redis", return_value=redis_client),
    ):
        response = _client(config).get("/ready")

    assert response.status_code == 200
    assert all(value == "ok" for value in response.json()["checks"].values())
