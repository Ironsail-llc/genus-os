"""Vault secret *values* are write-only from any human surface.

Before this suite, ``GET /api/vault/get`` returned the decrypted value to any
owner/admin browser session, and the Next.js BFF (``/api/bridge/[...path]``)
forwards every bridge path — so a signed-in operator's browser, any XSS on the
Helm, or anything holding a stolen session cookie could read raw credentials.

The bridge is not the place a human reads a secret back.  Only a verified
*service* token (``typ="service"``) — the engine calling on an agent's behalf —
may retrieve a value; humans administer secrets through the appliance CLI.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robothor.auth import tokens


@pytest.fixture(autouse=True)
def auth_key(monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "test-signing-key-at-least-32-bytes-long-xyz")
    tokens.reset_signing_key_cache()
    yield
    tokens.reset_signing_key_cache()


def _secure_mode(monkeypatch) -> None:
    monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
    monkeypatch.delenv("GENUS_AUTH_ENFORCE", raising=False)
    monkeypatch.setenv("ROBOTHOR_BRIDGE_HOST", "0.0.0.0")


SECRET_KEY = "payments/provider-token"
SECRET_VALUE = "sk-live-do-not-disclose"


@pytest.mark.parametrize("role", ["owner", "admin"])
@pytest.mark.asyncio
async def test_human_operator_session_cannot_read_a_secret_value(test_client, monkeypatch, role):
    """The exact path that leaked: an owner/admin browser session."""
    _secure_mode(monkeypatch)
    token = tokens.issue_access_token("human-1", "tenant-a", role)

    with patch("robothor.vault.get", return_value=SECRET_VALUE) as vault_get:
        response = await test_client.get(
            f"/api/vault/get?key={SECRET_KEY}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403
    # The vault is never even consulted — no decryption, no plaintext in memory.
    vault_get.assert_not_called()
    body = response.text
    assert SECRET_VALUE not in body
    assert "write-only" in response.json()["error"]


@pytest.mark.asyncio
async def test_service_token_still_retrieves_the_value(test_client, monkeypatch):
    """The engine's own service-to-service read must keep working."""
    _secure_mode(monkeypatch)
    monkeypatch.setattr("middleware.check_endpoint_access", lambda *a, **k: True)
    token = tokens.issue_service_token(
        "email-classifier",
        "tenant-a",
        agent_id="email-classifier",
        scopes=("bridge:read", "vault:read"),
    )

    with patch("robothor.vault.get", return_value=SECRET_VALUE) as vault_get:
        response = await test_client.get(
            f"/api/vault/get?key={SECRET_KEY}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json() == {"key": SECRET_KEY, "value": SECRET_VALUE}
    vault_get.assert_called_once_with(SECRET_KEY, tenant_id="tenant-a")


@pytest.mark.asyncio
async def test_missing_secret_still_404s_for_a_service_token(test_client, monkeypatch):
    _secure_mode(monkeypatch)
    monkeypatch.setattr("middleware.check_endpoint_access", lambda *a, **k: True)
    token = tokens.issue_service_token(
        "email-classifier",
        "tenant-a",
        agent_id="email-classifier",
        scopes=("bridge:read", "vault:read"),
    )

    with patch("robothor.vault.get", return_value=None):
        response = await test_client.get(
            "/api/vault/get?key=nope",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_the_refusal_is_audited_without_the_secret_value(test_client, monkeypatch):
    _secure_mode(monkeypatch)
    token = tokens.issue_access_token("human-1", "tenant-a", "owner")

    with (
        patch("routers._audit.log_event") as log_event,
        patch("robothor.vault.get", return_value=SECRET_VALUE),
    ):
        await test_client.get(
            f"/api/vault/get?key={SECRET_KEY}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert log_event.call_count == 1
    kwargs = log_event.call_args.kwargs
    assert log_event.call_args.args[0] == "vault.read.denied"
    assert kwargs["status"] == "denied"
    assert kwargs["actor"] == "human-1"
    assert SECRET_VALUE not in repr(log_event.call_args)


@pytest.mark.asyncio
async def test_vault_list_returns_key_names_only(test_client, monkeypatch):
    """``/api/vault/list`` is left reachable to operators *because* it never
    returns a value — assert that contract rather than assuming it."""
    _secure_mode(monkeypatch)
    token = tokens.issue_access_token("human-1", "tenant-a", "owner")

    with patch("robothor.vault.list", return_value=[SECRET_KEY]):
        response = await test_client.get(
            "/api/vault/list",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json() == {"keys": [SECRET_KEY]}
    assert SECRET_VALUE not in response.text
