"""Security tests for the Engine-owned dashboard completion boundary."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import APIRouter
from starlette.testclient import TestClient

from robothor.auth import tokens
from robothor.engine.dashboards.completions import (
    DEFAULT_DASHBOARD_MODEL,
    MAX_SYSTEM_PROMPT_CHARS,
    MAX_USER_PROMPT_CHARS,
)


@pytest.fixture
def completion_client(monkeypatch, engine_config):
    monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
    monkeypatch.delenv("DASHBOARD_MODEL", raising=False)
    monkeypatch.setenv("ROBOTHOR_ENGINE_HOST", "0.0.0.0")
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "dashboard-test-key-that-is-at-least-32-bytes")
    tokens.reset_signing_key_cache()

    from robothor.engine.health import create_health_app

    with (
        patch("robothor.engine.dashboards.get_dashboard_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_public_router", return_value=APIRouter()),
        patch("robothor.engine.webhooks.get_webhook_router", return_value=APIRouter()),
    ):
        app = create_health_app(engine_config)
    return TestClient(app, raise_server_exceptions=False), engine_config


def _headers(tenant_id: str, *, scopes: tuple[str, ...] = ("engine:chat",)) -> dict[str, str]:
    token = tokens.issue_access_token(
        "dashboard-member",
        tenant_id,
        "member",
        scopes=scopes,
    )
    return {"Authorization": f"Bearer {token}"}


def _response(content: str) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _payload(purpose: str = "render") -> dict[str, str]:
    return {
        "purpose": purpose,
        "system_prompt": "Return a safe dashboard.",
        "user_prompt": "Show current service health.",
    }


def test_unauthenticated_completion_is_denied_before_provider(completion_client) -> None:
    client, _ = completion_client
    with patch("robothor.engine.dashboards.completions.llm_call", new_callable=AsyncMock) as call:
        response = client.post("/api/dashboard/completions", json=_payload())

    assert response.status_code == 401
    call.assert_not_awaited()


def test_member_chat_scope_can_complete_with_fixed_render_settings(completion_client) -> None:
    client, config = completion_client
    with patch(
        "robothor.engine.dashboards.completions.llm_call",
        new_callable=AsyncMock,
        return_value=_response("<div>healthy</div>"),
    ) as call:
        response = client.post(
            "/api/dashboard/completions",
            json=_payload(),
            headers=_headers(config.tenant_id),
        )

    assert response.status_code == 200
    assert response.json() == {"content": "<div>healthy</div>"}
    assert call.await_args.args[0] == [
        {"role": "system", "content": "Return a safe dashboard."},
        {"role": "user", "content": "Show current service health."},
    ]
    assert call.await_args.kwargs == {
        "model": DEFAULT_DASHBOARD_MODEL,
        "temperature": 0.3,
        "json_mode": False,
        "timeout": 120,
        "max_retries": 1,
        "max_tokens": 4096,
    }


def test_wrong_tenant_is_denied_before_provider(completion_client) -> None:
    client, _ = completion_client
    with patch("robothor.engine.dashboards.completions.llm_call", new_callable=AsyncMock) as call:
        response = client.post(
            "/api/dashboard/completions",
            json=_payload(),
            headers=_headers("another-tenant"),
        )

    assert response.status_code == 403
    call.assert_not_awaited()


def test_triage_settings_and_operator_model_are_server_controlled(
    completion_client, monkeypatch
) -> None:
    client, config = completion_client
    monkeypatch.setenv("DASHBOARD_MODEL", "google/operator-selected-model")
    with patch(
        "robothor.engine.dashboards.completions.llm_call",
        new_callable=AsyncMock,
        return_value=_response('{"shouldUpdate":false}'),
    ) as call:
        response = client.post(
            "/api/dashboard/completions",
            json=_payload("triage"),
            headers=_headers(config.tenant_id),
        )

    assert response.status_code == 200
    assert call.await_args.kwargs == {
        "model": "openrouter/google/operator-selected-model",
        "temperature": 0.1,
        "json_mode": True,
        "timeout": 15,
        "max_retries": 1,
        "max_tokens": 256,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        {"system_prompt": "x" * (MAX_SYSTEM_PROMPT_CHARS + 1)},
        {"user_prompt": "x" * (MAX_USER_PROMPT_CHARS + 1)},
        {"model": "client-selected/model"},
        {"max_tokens": "999999"},
        {"purpose": "arbitrary"},
    ],
)
def test_request_bounds_and_client_model_selection_fail_closed(
    completion_client, mutation: dict[str, str]
) -> None:
    client, config = completion_client
    payload = _payload()
    payload.update(mutation)
    with patch("robothor.engine.dashboards.completions.llm_call", new_callable=AsyncMock) as call:
        response = client.post(
            "/api/dashboard/completions",
            json=payload,
            headers=_headers(config.tenant_id),
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid dashboard completion request"}
    call.assert_not_awaited()


def test_provider_failure_response_is_generic(completion_client) -> None:
    client, config = completion_client
    provider_detail = "upstream leaked sk-provider-secret and full prompt"
    with patch(
        "robothor.engine.dashboards.completions.llm_call",
        new_callable=AsyncMock,
        side_effect=RuntimeError(provider_detail),
    ):
        response = client.post(
            "/api/dashboard/completions",
            json=_payload(),
            headers=_headers(config.tenant_id),
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "dashboard completion unavailable"}
    assert provider_detail not in response.text
