"""Security tests for the Engine-owned dashboard completion boundary."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from fastapi import APIRouter
from starlette.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

from robothor.auth import tokens
from robothor.engine import config as defaults_config
from robothor.engine.dashboards import completions
from robothor.engine.dashboards.completions import (
    DEFAULT_DASHBOARD_MODEL,
    MAX_SYSTEM_PROMPT_CHARS,
    MAX_USER_PROMPT_CHARS,
)


@pytest.fixture
def completion_client(monkeypatch, engine_config):
    monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
    monkeypatch.delenv("DASHBOARD_MODEL", raising=False)
    monkeypatch.delenv("ROBOTHOR_LAST_RESORT_MODEL", raising=False)
    # The model chain reads the fleet defaults from the engine's manifest dir;
    # point it at this test's empty one, never at a real instance.
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(engine_config.workspace))
    monkeypatch.setenv("ROBOTHOR_MANIFEST_DIR", str(engine_config.manifest_dir))
    defaults_config.reset_defaults_cache()
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
        "model": [DEFAULT_DASHBOARD_MODEL],
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
        "model": ["google/operator-selected-model"],
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


# ── The canvas model chain ────────────────────────────────────────────────
#
# The canvas used to dial one hard-wired OpenRouter model, and forced an
# ``openrouter/`` prefix onto any id it was given: an instance with keys for a
# different provider could never generate a view. The chain is now the
# operator's choice first, then whatever the fleet itself runs on.


@pytest.fixture
def fleet(tmp_path, monkeypatch) -> Path:
    """An empty manifest dir, with nothing configured anywhere else."""
    directory = tmp_path / "agents"
    directory.mkdir()
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("ROBOTHOR_MANIFEST_DIR", str(directory))
    monkeypatch.delenv("DASHBOARD_MODEL", raising=False)
    monkeypatch.delenv("ROBOTHOR_LAST_RESORT_MODEL", raising=False)
    defaults_config.reset_defaults_cache()
    yield directory
    defaults_config.reset_defaults_cache()


def _defaults(directory: Path, model_block: dict) -> None:
    (directory / "_defaults.yaml").write_text(yaml.safe_dump({"model": model_block}))
    defaults_config.reset_defaults_cache()


class TestDashboardModelChain:
    def test_with_nothing_configured_it_keeps_the_old_default(self, fleet) -> None:
        assert completions.dashboard_model_chain() == [DEFAULT_DASHBOARD_MODEL]

    def test_with_no_canvas_model_it_uses_the_fleet_primary(self, fleet) -> None:
        _defaults(fleet, {"primary": "vertex_ai/gemini-fast", "fallbacks": ["x/y"]})
        assert completions.dashboard_model_chain() == ["vertex_ai/gemini-fast"]

    def test_a_configured_canvas_model_is_tried_first_then_the_fleet_primary(
        self, fleet
    ) -> None:
        _defaults(fleet, {"primary": "anthropic/claude-big", "canvas": "vertex_ai/gemini-fast"})
        assert completions.dashboard_model_chain() == ["vertex_ai/gemini-fast", "anthropic/claude-big"]

    def test_the_last_resort_model_ends_the_chain(self, fleet, monkeypatch) -> None:
        _defaults(fleet, {"primary": "anthropic/claude-big", "canvas": "vertex_ai/gemini-fast"})
        monkeypatch.setenv("ROBOTHOR_LAST_RESORT_MODEL", "ollama_chat/local")
        assert completions.dashboard_model_chain() == [
            "vertex_ai/gemini-fast",
            "anthropic/claude-big",
            "ollama_chat/local",
        ]

    def test_the_last_resort_model_alone_is_a_chain(self, fleet, monkeypatch) -> None:
        monkeypatch.setenv("ROBOTHOR_LAST_RESORT_MODEL", "ollama_chat/local")
        assert completions.dashboard_model_chain() == ["ollama_chat/local"]

    def test_the_env_override_wins(self, fleet, monkeypatch) -> None:
        _defaults(fleet, {"primary": "anthropic/claude-big", "canvas": "vertex_ai/gemini-fast"})
        monkeypatch.setenv("DASHBOARD_MODEL", "gemini/override")
        monkeypatch.setenv("ROBOTHOR_LAST_RESORT_MODEL", "ollama_chat/local")
        assert completions.dashboard_model_chain() == [
            "gemini/override",
            "vertex_ai/gemini-fast",
            "anthropic/claude-big",
            "ollama_chat/local",
        ]

    def test_no_provider_prefix_is_forced(self, fleet, monkeypatch) -> None:
        monkeypatch.setenv("DASHBOARD_MODEL", "vertex_ai/x")
        assert completions.dashboard_model_chain() == ["vertex_ai/x"]

    def test_a_model_named_twice_is_tried_once(self, fleet, monkeypatch) -> None:
        _defaults(fleet, {"primary": "vertex_ai/gemini-fast", "canvas": "vertex_ai/gemini-fast"})
        monkeypatch.setenv("DASHBOARD_MODEL", "vertex_ai/gemini-fast")
        monkeypatch.setenv("ROBOTHOR_LAST_RESORT_MODEL", "vertex_ai/gemini-fast")
        assert completions.dashboard_model_chain() == ["vertex_ai/gemini-fast"]

    def test_a_null_canvas_model_means_the_fleet_default(self, fleet) -> None:
        _defaults(fleet, {"primary": "vertex_ai/gemini-fast", "canvas": None})
        assert completions.dashboard_model_chain() == ["vertex_ai/gemini-fast"]

    def test_a_rewritten_defaults_file_is_seen_after_a_reload(self, fleet) -> None:
        _defaults(fleet, {"primary": "anthropic/claude-big"})
        assert completions.dashboard_model_chain() == ["anthropic/claude-big"]
        _defaults(fleet, {"primary": "anthropic/claude-big", "canvas": "vertex_ai/gemini-fast"})
        assert completions.dashboard_model_chain() == ["vertex_ai/gemini-fast", "anthropic/claude-big"]

    def test_an_invalid_env_override_fails_closed(self, fleet, monkeypatch) -> None:
        monkeypatch.setenv("DASHBOARD_MODEL", "not a model id")
        with pytest.raises(RuntimeError):
            completions.dashboard_model_chain()


def test_the_endpoint_hands_the_whole_chain_to_llm_call(completion_client) -> None:
    client, config = completion_client
    _defaults(
        config.manifest_dir,
        {"primary": "anthropic/claude-big", "canvas": "vertex_ai/gemini-fast"},
    )
    with patch(
        "robothor.engine.dashboards.completions.llm_call",
        new_callable=AsyncMock,
        return_value=_response("<div>ok</div>"),
    ) as call:
        response = client.post(
            "/api/dashboard/completions",
            json=_payload(),
            headers=_headers(config.tenant_id),
        )

    assert response.status_code == 200
    call.assert_awaited_once()
    assert call.await_args.kwargs["model"] == ["vertex_ai/gemini-fast", "anthropic/claude-big"]
