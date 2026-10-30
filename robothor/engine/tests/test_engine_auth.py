"""Security regression tests for the Engine HTTP/WebSocket identity boundary."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from robothor.auth import tokens
from robothor.engine.auth import required_scope
from robothor.engine.models import AgentRun, RunStatus, TriggerType


@pytest.fixture
def secure_engine(monkeypatch, engine_config):
    monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
    monkeypatch.setenv("ROBOTHOR_ENGINE_HOST", "0.0.0.0")
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "engine-auth-test-key-that-is-at-least-32-bytes")
    tokens.reset_signing_key_cache()

    runner = MagicMock()
    runner.config = engine_config
    runner.execute = AsyncMock(
        return_value=AgentRun(
            status=RunStatus.COMPLETED,
            output_text="authenticated response",
            trigger_type=TriggerType.WEBCHAT,
        )
    )

    from robothor.engine.health import create_health_app

    with (
        patch("robothor.engine.dashboards.get_dashboard_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_public_router", return_value=APIRouter()),
        patch("robothor.engine.webhooks.get_webhook_router", return_value=APIRouter()),
        patch("robothor.engine.chat.load_all_sessions", return_value={}),
    ):
        app = create_health_app(engine_config, runner=runner)

    return TestClient(app, raise_server_exceptions=False), runner, engine_config


def _user_token(tenant_id: str, role: str = "member") -> str:
    return tokens.issue_access_token("human-1", tenant_id, role)


def _authorization(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_only_probe_routes_are_public(secure_engine) -> None:
    client, _, _ = secure_engine
    assert client.get("/live").status_code == 200
    assert client.get("/health").status_code == 401
    assert client.get("/runs").status_code == 401


def test_bridge_user_identity_is_accepted_and_threaded_to_runner(secure_engine) -> None:
    client, runner, config = secure_engine
    token = _user_token(config.tenant_id)

    response = client.post(
        "/chat/send",
        json={"session_key": "agent:main:primary", "message": "hello"},
        headers=_authorization(token),
    )

    assert response.status_code == 200
    kwargs = runner.execute.await_args.kwargs
    assert kwargs["tenant_id"] == config.tenant_id
    assert kwargs["user_id"] == "human-1"
    assert kwargs["user_role"] == "member"
    assert kwargs["trigger_type"] == TriggerType.WEBCHAT


def test_wrong_tenant_is_rejected_before_handler(secure_engine) -> None:
    client, runner, _ = secure_engine
    token = _user_token("other-tenant")

    response = client.get(
        "/chat/history?session_key=agent:main:primary",
        headers=_authorization(token),
    )

    assert response.status_code == 403
    assert not runner.execute.called


def test_bridge_service_token_cannot_be_replayed_at_engine(secure_engine) -> None:
    client, _, config = secure_engine
    token = tokens.issue_service_token(
        "bridge-worker",
        config.tenant_id,
        audience="genus-bridge",
        scopes=("engine:chat",),
    )

    response = client.get(
        "/chat/history?session_key=agent:main:primary",
        headers=_authorization(token),
    )

    assert response.status_code == 401


def test_dedicated_engine_service_token_is_narrowly_scoped(secure_engine) -> None:
    client, _, config = secure_engine
    token = tokens.issue_service_token(
        "dashboard-bff",
        config.tenant_id,
        audience="genus-engine",
        scopes=("engine:chat", "engine:read"),
    )

    history = client.get(
        "/chat/history?session_key=agent:main:primary",
        headers=_authorization(token),
    )
    control = client.post(
        "/api/runs/run-1/steer",
        json={"text": "do something"},
        headers=_authorization(token),
    )

    assert history.status_code == 200
    assert control.status_code == 403


def test_dashboard_completion_is_chat_scoped() -> None:
    assert required_scope("POST", "/api/dashboard/completions") == "engine:chat"


def test_ide_websocket_authenticates_before_accept(secure_engine) -> None:
    client, _, config = secure_engine
    with pytest.raises(WebSocketDisconnect) as denied:
        with client.websocket_connect("/ide/ws"):
            pass
    assert denied.value.code == 4401

    token = tokens.issue_access_token(
        "ide-user",
        config.tenant_id,
        "member",
        audience="genus-engine",
        scopes=("engine:chat",),
    )
    with client.websocket_connect("/ide/ws", headers=_authorization(token)) as websocket:
        websocket.send_json({"jsonrpc": "2.0", "id": 1, "method": "status/health"})
        response = websocket.receive_json()
    assert response["result"]["status"] == "ok"


# ---------------------------------------------------------------------------
# Plugin channels authenticate their own senders
# ---------------------------------------------------------------------------
#
# A plugin channel is mounted at /api/channels/<name>, and its senders (a
# storefront, an SMS provider, a support form) never hold an engine token. The
# channel proves each request itself (an HMAC, a shared token, a Bot Framework
# JWT). If the engine bearer gate runs first, every one of those senders is
# refused before the channel's own check is reached.

_CHANNEL_TOKEN = "storefront-shared-secret"


def _storefront_router() -> APIRouter:
    router = APIRouter()

    @router.post("/api/channels/storefront/messages")
    async def _receive(request: Request) -> Any:
        if request.headers.get("x-channel-token") != _CHANNEL_TOKEN:
            return JSONResponse({"message": "Unauthorized"}, status_code=401)
        return {"status": "accepted"}

    @router.get("/api/channels/storefront/status")
    async def _status() -> Any:
        return {"status": "ok"}

    return router


class _StorefrontChannel:
    name = "storefront"

    def __init__(self) -> None:
        self.inbound_router = _storefront_router()

    async def send(self, target: str, text: str, **kw: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


class _StorefrontEP:
    group = "genus.channels"
    name = "storefront-plugin"

    def load(self) -> dict:
        return {"genus_contract_version": "1.0", "channels": {"storefront": _StorefrontChannel()}}


@pytest.fixture
def engine_with_channel(monkeypatch, engine_config):
    """A secured engine with one armed plugin channel, ``storefront``."""
    from robothor.engine.channels import reset_channels
    from robothor.engine.channels.routers import reset_mounted
    from robothor.plugins import loader, reload_plugins
    from robothor.settings import reset_settings

    monkeypatch.setattr(loader, "_discover", lambda: [_StorefrontEP()])
    monkeypatch.setenv("ROBOTHOR_CHANNELS", "storefront")
    reset_settings()
    reload_plugins()
    reset_channels()
    reset_mounted()

    monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
    monkeypatch.setenv("ROBOTHOR_ENGINE_HOST", "0.0.0.0")
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "engine-auth-test-key-that-is-at-least-32-bytes")
    tokens.reset_signing_key_cache()

    runner = MagicMock()
    runner.config = engine_config

    from robothor.engine.health import create_health_app

    with (
        patch("robothor.engine.dashboards.get_dashboard_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_public_router", return_value=APIRouter()),
        patch("robothor.engine.webhooks.get_webhook_router", return_value=APIRouter()),
        patch("robothor.engine.chat.load_all_sessions", return_value={}),
    ):
        app = create_health_app(engine_config, runner=runner)

    yield TestClient(app, raise_server_exceptions=False)

    monkeypatch.setattr(loader, "_discover", list)
    monkeypatch.delenv("ROBOTHOR_CHANNELS", raising=False)
    reset_settings()
    reload_plugins()
    reset_channels()
    reset_mounted()


class TestAMountedChannelAuthenticatesItsOwnSenders:
    def test_the_channel_is_mounted(self, engine_with_channel) -> None:
        from robothor.engine.channels.routers import is_mounted

        assert is_mounted("storefront")

    def test_a_post_with_the_channels_own_credential_reaches_the_channel(
        self, engine_with_channel
    ) -> None:
        response = engine_with_channel.post(
            "/api/channels/storefront/messages",
            json={"text": "What are your hours?"},
            headers={"x-channel-token": _CHANNEL_TOKEN},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "accepted"}

    def test_a_post_the_channel_refuses_gets_the_channels_own_answer(
        self, engine_with_channel
    ) -> None:
        response = engine_with_channel.post(
            "/api/channels/storefront/messages",
            json={"text": "forged"},
            headers={"x-channel-token": "wrong"},
        )
        assert response.status_code == 401
        assert response.json() == {"message": "Unauthorized"}

    def test_a_get_on_a_mounted_channel_still_needs_the_engine_bearer(
        self, engine_with_channel
    ) -> None:
        response = engine_with_channel.get("/api/channels/storefront/status")
        assert response.status_code == 401
        assert response.json() == {"error": "authentication required"}

    def test_a_post_to_a_channel_that_is_not_mounted_still_needs_the_bearer(
        self, engine_with_channel
    ) -> None:
        response = engine_with_channel.post(
            "/api/channels/unmounted/messages",
            json={"text": "hello"},
            headers={"x-channel-token": _CHANNEL_TOKEN},
        )
        assert response.status_code == 401
        assert response.json() == {"error": "authentication required"}


class TestTheChannelExemptionIsNarrow:
    def test_only_a_post_under_a_mounted_channels_own_prefix_is_exempt(self, monkeypatch) -> None:
        from robothor.engine import auth
        from robothor.engine.channels import routers

        monkeypatch.setattr(routers, "is_mounted", lambda name: name == "storefront")

        assert auth.channel_uses_own_auth("POST", "/api/channels/storefront")
        assert auth.channel_uses_own_auth("POST", "/api/channels/storefront/messages")
        assert not auth.channel_uses_own_auth("GET", "/api/channels/storefront/messages")
        assert not auth.channel_uses_own_auth("PUT", "/api/channels/storefront/messages")
        assert not auth.channel_uses_own_auth("POST", "/api/channels/other/messages")
        assert not auth.channel_uses_own_auth("POST", "/api/channels/storefrontX/messages")
        assert not auth.channel_uses_own_auth("POST", "/api/channels")
        assert not auth.channel_uses_own_auth("POST", "/api/admin/channels")
        assert not auth.channel_uses_own_auth(
            "POST", "/api/channels/storefront/../../admin/channels"
        )
