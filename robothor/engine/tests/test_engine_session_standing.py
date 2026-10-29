"""The engine refuses a human token whose account no longer stands.

The dashboard forwards the user's own bridge token to the engine's ``/chat/*``
and ``/api/dashboard/completions``, and the IDE WebSocket takes it too. The
bridge already refuses a token whose account has left its home tenant or been
disabled (``robothor.auth.session_standing``); without the same check here a
disabled account kept engine chat until its token expired.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import APIRouter
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from robothor.auth import session_standing, tokens
from robothor.engine.models import AgentRun, RunStatus, TriggerType


@pytest.fixture
def secure_engine(monkeypatch, engine_config):
    monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
    monkeypatch.setenv("ROBOTHOR_ENGINE_HOST", "0.0.0.0")
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "engine-auth-test-key-that-is-at-least-32-bytes")
    tokens.reset_signing_key_cache()
    session_standing.forget_all()

    runner = MagicMock()
    runner.config = engine_config
    runner.execute = AsyncMock(
        return_value=AgentRun(
            status=RunStatus.COMPLETED, output_text="ok", trigger_type=TriggerType.WEBCHAT
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
    yield TestClient(app, raise_server_exceptions=False), engine_config
    session_standing.forget_all()


def _account(tenant: str, status: str = "active") -> dict:
    return {"id": "human-1", "tenant_id": tenant, "status": status}


def _send(client, token):
    return client.post(
        "/chat/send",
        json={"session_key": "agent:main:primary", "message": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_a_standing_account_chats(secure_engine):
    client, config = secure_engine
    token = tokens.issue_access_token("human-1", config.tenant_id, "member")
    with patch("robothor.auth.accounts.get_account_by_id", return_value=_account(config.tenant_id)):
        assert _send(client, token).status_code == 200


def test_a_disabled_account_is_refused(secure_engine):
    client, config = secure_engine
    token = tokens.issue_access_token("human-1", config.tenant_id, "member")
    with patch(
        "robothor.auth.accounts.get_account_by_id",
        return_value=_account(config.tenant_id, status="disabled"),
    ):
        response = _send(client, token)
    assert response.status_code == 401
    assert response.json() == {"error": "session no longer valid"}


def test_a_moved_account_is_refused(secure_engine):
    client, config = secure_engine
    token = tokens.issue_access_token("human-1", config.tenant_id, "member")
    with patch("robothor.auth.accounts.get_account_by_id", return_value=_account("elsewhere")):
        assert _send(client, token).status_code == 401


def test_a_service_token_is_not_looked_up(secure_engine):
    client, config = secure_engine
    token = tokens.issue_service_token(
        "dashboard-bff", config.tenant_id, audience="genus-engine", scopes=("engine:read",)
    )
    with patch("robothor.auth.accounts.get_account_by_id") as lookup:
        client.get(
            "/chat/history?session_key=agent:main:primary",
            headers={"Authorization": f"Bearer {token}"},
        )
    lookup.assert_not_called()


def test_the_ide_websocket_refuses_a_disabled_account(secure_engine):
    client, config = secure_engine
    token = tokens.issue_access_token("human-1", config.tenant_id, "owner")
    with (
        patch(
            "robothor.auth.accounts.get_account_by_id",
            return_value=_account(config.tenant_id, status="disabled"),
        ),
        pytest.raises(WebSocketDisconnect) as closed,
        client.websocket_connect("/ide/ws", headers={"Authorization": f"Bearer {token}"}),
    ):
        pass
    assert closed.value.code == 4401


def test_a_database_failure_is_not_a_sign_out(secure_engine):
    """Same bargain as the bridge: fail open, with a warning, uncached."""
    client, config = secure_engine
    token = tokens.issue_access_token("human-1", config.tenant_id, "member")
    with patch("robothor.auth.accounts.get_account_by_id", side_effect=OSError("down")):
        assert _send(client, token).status_code == 200


# ─── Acting tokens need tenant switching enforced ─────────────────────


@pytest.fixture
def switch_mode(monkeypatch):
    from robothor.settings import reset_settings

    def set_mode(value: str) -> None:
        monkeypatch.setenv("GENUS_TENANT_SWITCH_MODE", value)
        reset_settings()

    yield set_mode
    reset_settings()


def _acting(config) -> str:
    return tokens.issue_access_token(
        "human-1", config.tenant_id, "admin", home_tenant_id="acme-parent"
    )


@pytest.mark.parametrize("value", ["off", "observe", ""])
def test_an_acting_token_is_refused_unless_switching_is_enforced(secure_engine, switch_mode, value):
    client, config = secure_engine
    switch_mode(value)
    with patch("robothor.auth.accounts.get_account_by_id", return_value=_account("acme-parent")):
        response = _send(client, _acting(config))
    assert response.status_code == 403
    assert response.json() == {"error": "tenant switching disabled"}


def test_an_acting_token_is_honoured_under_enforce(secure_engine, switch_mode):
    client, config = secure_engine
    switch_mode("enforce")
    with patch("robothor.auth.accounts.get_account_by_id", return_value=_account("acme-parent")):
        assert _send(client, _acting(config)).status_code == 200


@pytest.mark.parametrize("value", ["off", "observe"])
def test_a_home_token_is_unaffected(secure_engine, switch_mode, value):
    client, config = secure_engine
    switch_mode(value)
    token = tokens.issue_access_token("human-1", config.tenant_id, "member")
    with patch("robothor.auth.accounts.get_account_by_id", return_value=_account(config.tenant_id)):
        assert _send(client, token).status_code == 200


def test_a_service_token_is_unaffected(secure_engine, switch_mode):
    client, config = secure_engine
    switch_mode("off")
    token = tokens.issue_service_token(
        "dashboard-bff", config.tenant_id, audience="genus-engine", scopes=("engine:read",)
    )
    response = client.get(
        "/chat/history?session_key=agent:main:primary",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200


def test_the_ide_websocket_closes_4403_for_an_acting_token_with_switching_off(
    secure_engine, switch_mode
):
    client, config = secure_engine
    switch_mode("off")
    with (
        patch("robothor.auth.accounts.get_account_by_id", return_value=_account("acme-parent")),
        pytest.raises(WebSocketDisconnect) as closed,
        client.websocket_connect("/ide/ws", headers={"Authorization": f"Bearer {_acting(config)}"}),
    ):
        pass
    assert closed.value.code == 4403


def test_the_chat_router_mounted_alone_refuses_it_too(secure_engine, switch_mode, monkeypatch):
    """The router's own dependency, without the app's middleware in front of it."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from robothor.engine import chat

    _client, config = secure_engine
    switch_mode("off")
    monkeypatch.setattr(chat, "_config", config)
    request = SimpleNamespace(
        state=SimpleNamespace(),
        url=SimpleNamespace(path="/chat/send"),
        method="POST",
        headers={"authorization": f"Bearer {_acting(config)}"},
    )
    with pytest.raises(HTTPException) as refused:
        chat._require_chat_auth(request)
    assert refused.value.status_code == 403


# ─── The 4403 close must reach the client as a WebSocket close ────────


def _asgi_websocket_messages(app, token: str) -> list[dict]:
    """Drive ``/ide/ws`` at the ASGI level and return what the app SENT.

    Starlette's TestClient reports a close sent before ``accept`` as that close
    code, but a real server (uvicorn) turns a pre-accept close into an HTTP 403
    handshake rejection — the client never sees 4403 or its reason. Reading the
    raw ASGI messages is what tells the two apart.
    """
    import asyncio

    sent: list[dict] = []
    inbox = [{"type": "websocket.connect"}]

    async def receive():
        if inbox:
            return inbox.pop(0)
        await asyncio.sleep(0)
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": "/ide/ws",
        "raw_path": b"/ide/ws",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver"), (b"authorization", f"Bearer {token}".encode())],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
        "state": {},
    }
    asyncio.run(app(scope, receive, send))
    return sent


def test_the_switching_refusal_is_accepted_then_closed_4403(secure_engine, switch_mode):
    client, config = secure_engine
    switch_mode("off")
    with patch("robothor.auth.accounts.get_account_by_id", return_value=_account("acme-parent")):
        sent = _asgi_websocket_messages(client.app, _acting(config))
    kinds = [m["type"] for m in sent]
    assert kinds[:2] == ["websocket.accept", "websocket.close"], sent
    assert sent[1]["code"] == 4403
    assert sent[1].get("reason") == "tenant switching disabled"
