"""`/ready` and the trigger route under `enforce`.

Two holes, one cause: a schema-refused manifest is a BROKEN agent, and both of
these treated it as an absent one — `/ready` because `check_fleet` built its id
set from `load_all_manifests`, which drops every failure bucket identically, and
the trigger route because it called `load_agent_config` with no guard at all and
turned the raise into a 500.

"Broken" and "absent" are the distinction the whole manifest-scan machinery
exists to preserve. An agent reported missing gets re-created; an agent reported
broken gets its manifest fixed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import APIRouter
from starlette.testclient import TestClient

from robothor.engine.config import EngineConfig

GOOD = """\
id: main
name: Main
description: A generic fixture agent
version: "2026-09-11"
department: core
"""

BROKEN = """\
id: bob
name: Bob
description: A generic fixture agent
version: "2026-09-11"
department: opperations
"""


def _client(config: EngineConfig, runner: object | None = None) -> TestClient:
    from robothor.engine.health import create_health_app

    with (
        patch("robothor.engine.dashboards.get_dashboard_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_public_router", return_value=APIRouter()),
        patch("robothor.engine.webhooks.get_webhook_router", return_value=APIRouter()),
    ):
        return TestClient(create_health_app(config, runner=runner), raise_server_exceptions=False)


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "enforce")
    (tmp_path / "main.yaml").write_text(GOOD)
    (tmp_path / "bob.yaml").write_text(BROKEN)
    return tmp_path


def _ready(config: EngineConfig):
    redis_client = MagicMock()
    redis_client.ping = AsyncMock(return_value=True)
    redis_client.aclose = AsyncMock()
    with (
        patch("robothor.db.connection.get_connection"),
        patch("robothor.engine.tracking.list_schedules", return_value=[]),
        patch("robothor.federation.connections.load_connections", return_value=[]),
        patch("redis.asyncio.Redis", return_value=redis_client),
    ):
        return _client(config).get("/ready")


class TestReadyDistinguishesBrokenFromAbsent:
    def test_a_schema_broken_agent_makes_readiness_degraded(self, fleet):
        config = EngineConfig(
            workspace=fleet.parent,
            manifest_dir=fleet,
            allow_empty_fleet=False,
            required_agent_ids=("main",),
        )
        response = _ready(config)

        assert response.status_code == 503
        body = response.json()
        assert body["checks"]["fleet"] == "error:broken:1"
        assert body["broken_agents"] == ["bob"]

    def test_the_same_fleet_is_ready_when_nothing_is_broken(self, fleet, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "off")
        config = EngineConfig(
            workspace=fleet.parent,
            manifest_dir=fleet,
            allow_empty_fleet=False,
            required_agent_ids=("main",),
        )
        response = _ready(config)

        assert response.status_code == 200
        assert response.json()["checks"]["fleet"] == "ok"
        assert response.json().get("broken_agents", []) == []

    def test_a_deleted_agent_still_reads_as_missing_not_broken(self, tmp_path, monkeypatch):
        """The discriminator. Absent must keep saying absent, or the new
        message is just a rename of the old one."""
        monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "enforce")
        (tmp_path / "bob.yaml").write_text(BROKEN.replace("opperations", "operations"))
        config = EngineConfig(
            workspace=tmp_path.parent,
            manifest_dir=tmp_path,
            allow_empty_fleet=False,
            required_agent_ids=("main",),
        )
        response = _ready(config)

        assert response.status_code == 503
        assert "missing" in response.json()["checks"]["fleet"]
        assert response.json().get("broken_agents", []) == []


class TestTriggerRoute:
    """Driven through the handler, so these stay about the schema guard alone.

    The route itself is exercised over HTTP in
    `test_health_route_annotations.py`, which pins the separate defect found on
    the way here: `Request` was imported inside `create_health_app`, so under
    `from __future__ import annotations` FastAPI could not resolve it and
    answered 422 to every caller. That is fixed in this branch. Calling the
    handler keeps the two concerns in separate files — a change to either the
    routing or the guard fails exactly one of them.
    """

    def _handler(self, config):
        client = _client(config, runner=MagicMock())
        for route in client.app.routes:
            if getattr(route, "path", None) == "/api/agents/{agent_id}/trigger":
                return route.endpoint
        raise AssertionError("trigger route is not registered at all")

    @pytest.mark.asyncio
    async def test_a_schema_broken_agent_returns_an_error_dict_not_a_raise(self, fleet):
        config = EngineConfig(workspace=fleet.parent, manifest_dir=fleet)
        body = await self._handler(config)("bob", None)

        assert "error" in body
        assert "bob" in body["error"]
        assert "SchemaError" in body["error"] or "schema" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_a_genuinely_absent_agent_keeps_its_own_message(self, fleet):
        """Broken and absent must not collapse into one string here either."""
        config = EngineConfig(workspace=fleet.parent, manifest_dir=fleet)
        body = await self._handler(config)("nobody", None)

        assert body["error"] == "Agent not found: nobody"
