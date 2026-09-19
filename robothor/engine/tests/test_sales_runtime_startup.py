"""The daemon's scheduler boot and readiness routes consume native sales state."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import APIRouter
from starlette.testclient import TestClient

from robothor.engine.config import EngineConfig
from robothor.engine.scheduler import CronScheduler


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_scheduler_bootstraps_managed_sales_and_keeps_core_alive(
    tmp_path, monkeypatch, failure
):
    import robothor.engine.scheduler as module

    config = EngineConfig(workspace=tmp_path, manifest_dir=tmp_path)
    scheduler = CronScheduler(config, None)
    called = asyncio.Event()

    async def bootstrap():
        assert scheduler.scheduler.running
        called.set()
        if failure:
            raise ValueError("Managed artifact unavailable")

    scheduler.sales_runtime = SimpleNamespace(bootstrap=bootstrap)
    monkeypatch.setattr(module, "load_manifest_dir", lambda _: SimpleNamespace(manifests=[]))
    monkeypatch.setattr(module, "alert_manifest_scan", AsyncMock())
    monkeypatch.setattr(scheduler, "register_plugin_jobs", Mock())
    monkeypatch.setattr(scheduler, "_catch_up_missed_runs", Mock())
    monkeypatch.setattr("robothor.memory.projection.projection_enabled", lambda: False)
    task = asyncio.create_task(scheduler.start())
    try:
        await asyncio.wait_for(called.wait(), timeout=1)
        assert scheduler.scheduler.running
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        if scheduler.scheduler.running:
            scheduler.scheduler.shutdown(wait=False)


@pytest.mark.parametrize("failure", [False, True])
def test_engine_readiness_includes_native_sales_verification(tmp_path, failure):
    from robothor.engine.health import create_health_app

    config = EngineConfig(workspace=tmp_path, manifest_dir=tmp_path, allow_empty_fleet=True)
    runtime = SimpleNamespace(
        readiness=AsyncMock(
            side_effect=ValueError("Pending deployment") if failure else None, return_value="ok"
        )
    )
    scheduler = SimpleNamespace(sales_runtime=runtime)
    redis = SimpleNamespace(ping=AsyncMock(return_value=True), aclose=AsyncMock())
    with (
        patch("robothor.engine.dashboards.get_dashboard_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_public_router", return_value=APIRouter()),
        patch("robothor.engine.webhooks.get_webhook_router", return_value=APIRouter()),
        patch("robothor.db.connection.get_connection"),
        patch("robothor.engine.tracking.list_schedules", return_value=[]),
        patch("robothor.federation.connections.load_connections", return_value=[]),
        patch("redis.asyncio.Redis", return_value=redis),
    ):
        client = TestClient(create_health_app(config, scheduler=scheduler))
        response = client.get("/ready")
    assert response.status_code == (503 if failure else 200)
    assert response.json()["checks"]["sales_runtime"] == (
        "error:Pending deployment" if failure else "ok"
    )
    runtime.readiness.assert_awaited_once()
