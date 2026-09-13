"""The engine's scheduler admin surface.

Two properties carry the weight here:

1. **A reconcile route with no scheduler must say so.** ``create_health_app``
   is built by tests and by ``robothor.api`` with no scheduler at all, and a
   route that quietly answered "reconciled, nothing changed" in that state
   would be the inert control this codebase has shipped six times — green, and
   worth nothing.
2. **``/api/admin`` means ``engine:control``, reads included.** ``GET
   /api/admin/tools`` enumerates the engine's whole tool surface; a read-scoped
   dashboard token is the wrong identity for it, and the requirement has to
   come from the prefix rather than from anyone remembering.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import APIRouter
from starlette.testclient import TestClient

from robothor.engine.schedule_reconcile import ReconcileResult


def _make_app(scheduler=None):
    mock_config = MagicMock()
    mock_config.tenant_id = "test-tenant"
    mock_config.bot_token = ""
    mock_config.port = 18800

    from robothor.engine.health import create_health_app

    with (
        patch("robothor.engine.dashboards.get_dashboard_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_public_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_completion_router", return_value=APIRouter()),
        patch("robothor.engine.webhooks.get_webhook_router", return_value=APIRouter()),
        patch("robothor.db.connection.get_connection"),
    ):
        return create_health_app(mock_config, runner=None, scheduler=scheduler)


def _client(scheduler=None):
    return TestClient(_make_app(scheduler), raise_server_exceptions=False)


@pytest.fixture
def fake_scheduler():
    """A scheduler whose ``reconcile`` reports one add and one refusal."""
    scheduler = MagicMock()
    scheduler.reconcile = AsyncMock(
        return_value=ReconcileResult(
            added=["demo-agent"],
            replaced=[],
            pruned=["retired-agent"],
            blocked={"broken-agent": "SchemaError"},
            clean=False,
        )
    )
    return scheduler


class TestReconcileRoute:
    def test_it_answers_with_every_count(self, fake_scheduler):
        body = _client(fake_scheduler).post("/api/admin/scheduler/reconcile").json()

        assert set(body) == {"added", "replaced", "pruned", "refreshed", "blocked", "clean"}
        assert body["added"] == ["demo-agent"]
        assert body["pruned"] == ["retired-agent"]
        assert body["blocked"] == {"broken-agent": "SchemaError"}
        assert body["clean"] is False

    def test_it_calls_the_live_scheduler(self, fake_scheduler):
        _client(fake_scheduler).post("/api/admin/scheduler/reconcile")

        fake_scheduler.reconcile.assert_awaited_once()

    def test_no_scheduler_is_a_503_and_not_a_quiet_success(self):
        """ "Nothing to reconcile" and "nothing reconciled anything" must not
        share a response. The caller decides whether to retry."""
        response = _client(None).post("/api/admin/scheduler/reconcile")

        assert response.status_code == 503
        assert response.json() == {"error": "scheduler not running"}

    def test_it_falls_back_to_the_daemons_own_handle(self, fake_scheduler):
        """``robothor.api`` builds the health app without a scheduler argument;
        the daemon's module handle is the same one SIGHUP already uses."""
        from robothor.engine import daemon

        with patch.object(daemon, "_ACTIVE_SCHEDULER", fake_scheduler):
            body = _client(None).post("/api/admin/scheduler/reconcile").json()

        assert body["added"] == ["demo-agent"]

    def test_the_response_names_no_path_and_no_manifest_value(self, fake_scheduler):
        """Rules 1 and 2 — this body reaches a browser through the bridge."""
        body = _client(fake_scheduler).post("/api/admin/scheduler/reconcile").json()

        rendered = repr(body)
        assert "/" not in rendered, "a path reached the reconcile response"
        assert ".yaml" not in rendered, "a manifest filename reached the reconcile response"


class TestToolsRoute:
    def test_it_lists_the_registry_names(self):
        body = _client().get("/api/admin/tools").json()

        assert body["count"] == len(body["tools"])
        assert body["tools"] == sorted(body["tools"])
        # A registry that answered nothing would make the bridge's tool check
        # vacuously pass on every manifest.
        assert "read_file" in body["tools"]

    def test_it_agrees_with_the_registry_it_claims_to_read(self):
        from robothor.engine.tools import ToolRegistry

        body = _client().get("/api/admin/tools").json()

        assert set(body["tools"]) == set(ToolRegistry()._schemas)


class TestScopeEnforcement:
    """``/api/admin`` requires ``engine:control``, by prefix."""

    @pytest.fixture
    def _signed_tokens(self, monkeypatch):
        from robothor.auth import tokens

        monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
        monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "test-signing-key-at-least-32-bytes-long")
        tokens.reset_signing_key_cache()
        yield tokens
        tokens.reset_signing_key_cache()

    def _token(self, tokens, scopes):
        from robothor.engine.auth import ENGINE_AUDIENCE

        return tokens.issue_service_token(
            "genus-bridge",
            "test-tenant",
            audience=ENGINE_AUDIENCE,
            scopes=scopes,
            ttl_seconds=60,
        )

    def test_a_read_scoped_token_cannot_reconcile(self, _signed_tokens, fake_scheduler):
        token = self._token(_signed_tokens, ("engine:read",))

        response = _client(fake_scheduler).post(
            "/api/admin/scheduler/reconcile",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 403
        fake_scheduler.reconcile.assert_not_awaited()

    def test_a_read_scoped_token_cannot_list_tools(self, _signed_tokens):
        token = self._token(_signed_tokens, ("engine:read",))

        response = _client().get("/api/admin/tools", headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 403

    def test_an_unauthenticated_request_is_refused(self, _signed_tokens, fake_scheduler):
        response = _client(fake_scheduler).post("/api/admin/scheduler/reconcile")

        assert response.status_code == 401

    def test_a_control_scoped_token_is_admitted(self, _signed_tokens, fake_scheduler):
        token = self._token(_signed_tokens, ("engine:control",))

        response = _client(fake_scheduler).post(
            "/api/admin/scheduler/reconcile",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        fake_scheduler.reconcile.assert_awaited_once()
