"""The engine's log_interaction bridge calls carry a verifiable service token.

Since the bridge went fail-closed (PR #176), every credential-less POST to
/log-interaction has 401'd. These tests pin the fix: the engine mints a
short-lived service token whose claims pass the bridge's own ``verify_token``
with the audience and scopes its middleware requires (``bridge:write`` for
POSTs plus the narrow ``integration:write`` for /log-interaction), and the
token is cached until shortly before expiry rather than minted per call.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from robothor.auth import tokens as auth_tokens
from robothor.engine.tools import service_client
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.service_client import bridge_headers, reset_bridge_token_cache


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "unit-test-signing-key-0123456789abcdef0123")
    auth_tokens.reset_signing_key_cache()
    reset_bridge_token_cache()
    yield
    auth_tokens.reset_signing_key_cache()
    reset_bridge_token_cache()


def _token_from(headers: dict[str, str]) -> str:
    assert headers["Authorization"].startswith("Bearer ")
    return headers["Authorization"].removeprefix("Bearer ")


def test_minted_token_passes_bridge_verify_with_required_scopes():
    from robothor.auth.deps import verify_token

    headers = bridge_headers("engine:test-agent", "test-tenant")
    auth = verify_token(_token_from(headers), expected_audience="genus-bridge")

    assert auth.is_service
    assert auth.tenant_id == "test-tenant"
    assert auth.has_scope("bridge:read")
    assert auth.has_scope("bridge:write")
    assert auth.has_scope("integration:write")


def test_minted_token_clears_bridge_authorization_gate_for_log_interaction():
    """The bridge's own route-authorization check must not deny the token."""
    from crm.bridge.middleware import _authorization_denial
    from robothor.auth.deps import verify_token

    headers = bridge_headers("engine:test-agent", "test-tenant")
    auth = verify_token(_token_from(headers), expected_audience="genus-bridge")
    assert _authorization_denial(auth, "POST", "/log-interaction") is None


def test_token_without_integration_scope_is_denied_for_log_interaction():
    from crm.bridge.middleware import _authorization_denial
    from robothor.auth.deps import verify_token

    token = auth_tokens.issue_service_token(
        "engine:test-agent",
        "test-tenant",
        audience="genus-bridge",
        scopes=("bridge:read", "bridge:write"),
    )
    auth = verify_token(token, expected_audience="genus-bridge")
    assert _authorization_denial(auth, "POST", "/log-interaction") is not None


def test_token_is_cached_until_near_expiry(monkeypatch: pytest.MonkeyPatch):
    first = _token_from(bridge_headers("engine:test-agent", "test-tenant"))
    second = _token_from(bridge_headers("engine:test-agent", "test-tenant"))
    assert first == second

    # Jump to within 60s of expiry: the cache must re-mint.
    import time as _time

    real_time = _time.time
    monkeypatch.setattr(
        service_client.time,
        "time",
        lambda: real_time() + auth_tokens.SERVICE_TTL_SECONDS - 30,
    )
    third = _token_from(bridge_headers("engine:test-agent", "test-tenant"))
    assert third != first


def test_cache_is_per_tenant():
    a = _token_from(bridge_headers("engine:test-agent", "tenant-a"))
    b = _token_from(bridge_headers("engine:test-agent", "tenant-b"))
    assert a != b


async def test_log_interaction_handler_sends_bearer_token():
    from robothor.engine.tools.handlers.vision import HANDLERS

    captured: dict[str, str] = {}

    async def _fake_call_service(service, method, url, *, json=None, headers=None, timeout=10.0):
        captured.update(headers or {})
        return {"success": True}

    with patch(
        "robothor.engine.tools.handlers.vision.call_service",
        new=AsyncMock(side_effect=_fake_call_service),
    ):
        ctx = ToolContext(agent_id="main", tenant_id="test-tenant")
        result = await HANDLERS["log_interaction"](
            {"contact_name": "Alice", "channel": "telegram", "direction": "inbound"}, ctx
        )

    assert result == {"success": True}
    from robothor.auth.deps import verify_token

    auth = verify_token(
        captured["Authorization"].removeprefix("Bearer "), expected_audience="genus-bridge"
    )
    assert auth.user_id == "engine:main"
    assert auth.has_scope("integration:write")
