"""The operator must be able to see which economics are in force.

2026-08-27. The fleet ran 29 hours on the local tier and the only way anyone
could tell was to query the database for which model served the last steps.
/health reported agents, schedules and a version, but nothing about the mode
the engine was actually operating under.

A profile that cannot be inspected is indistinguishable from a guess, so each
number reports its own source: probed, configured, or default.
"""

from unittest.mock import MagicMock, patch

from fastapi import APIRouter
from starlette.testclient import TestClient


def _health():
    """Same construction the existing health tests use."""
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
    client = TestClient(app, raise_server_exceptions=False)
    return client.get("/health").json()


class TestExecutionModeIsVisible:
    def test_health_reports_the_mode_in_force(self):
        block = _health()["execution_mode"]
        assert block["mode"] in ("cloud", "local")
        assert block["source"] in ("observed", "override")

    def test_health_reports_the_policy_that_follows_from_it(self):
        policy = _health()["execution_mode"]["policy"]
        assert policy["max_concurrent_runs"] >= 1
        assert isinstance(policy["monetary_governor"], bool)

    def test_every_host_number_says_where_it_came_from(self):
        """Otherwise a default is indistinguishable from a measurement."""
        host = _health()["execution_mode"]["host"]
        for field, reading in host.items():
            assert reading["source"] in ("probed", "configured", "default"), field

    def test_a_probe_failure_never_takes_down_the_health_endpoint(self, engine_config, monkeypatch):
        import robothor.engine.host_profile as hp

        def boom():
            raise RuntimeError("sensor exploded")

        monkeypatch.setattr(hp, "detect_host_profile", boom)
        body = _health()
        assert body["status"] == "healthy"
        assert body["execution_mode"]["available"] is False
