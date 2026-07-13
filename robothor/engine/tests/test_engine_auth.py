"""Security regression tests for the Engine HTTP/WebSocket identity boundary."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import APIRouter
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
