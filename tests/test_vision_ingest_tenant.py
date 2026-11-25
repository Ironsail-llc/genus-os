"""The vision service proves its tenant when it posts an event to the orchestrator.

It is one process per instance, bound to the tenant its environment names
(``ROBOTHOR_TENANT_ID``, then ``ROBOTHOR_DEFAULT_TENANT``). The orchestrator
takes a tenant only from a verified token, so the service sends a minted
service token for that tenant -- the same mint the engine uses for its bridge
calls. A process that cannot mint (no signing key) or is bound to no tenant
posts with no token, which the orchestrator accepts only on a single-tenant
deployment.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.vision.service import VisionService

KEY = "test-signing-key-at-least-32-bytes-long-xyz"


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    from robothor.auth import tokens
    from robothor.engine.tools.service_client import reset_bridge_token_cache

    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", KEY)
    tokens.reset_signing_key_cache()
    reset_bridge_token_cache()
    yield
    tokens.reset_signing_key_cache()
    reset_bridge_token_cache()


@pytest.fixture
def service(tmp_path):
    dirs = [tmp_path / d for d in ("snapshots", "faces", "state")]
    for d in dirs:
        d.mkdir()
    return VisionService(
        orchestrator_url="http://orchestrator.invalid",
        snapshot_dir=dirs[0],
        face_data_dir=dirs[1],
        state_dir=dirs[2],
    )


def _capture_post():
    client = MagicMock()
    client.post = AsyncMock(return_value=MagicMock(status_code=200))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _sent_headers(client):
    return client.post.await_args.kwargs.get("headers") or {}


async def test_an_event_carries_a_token_the_orchestrator_verifies_as_the_process_tenant(
    service, monkeypatch
):
    from robothor.auth.deps import bearer_from_header, verify_token

    monkeypatch.setenv("ROBOTHOR_TENANT_ID", "tenant-a")
    monkeypatch.setenv("ROBOTHOR_DEFAULT_TENANT", "tenant-a")
    client = _capture_post()
    with patch("httpx.AsyncClient", return_value=client):
        await service.ingest_event("Alice arrived", {"camera": "front"})
    token = bearer_from_header(_sent_headers(client).get("Authorization"))
    assert token, "the event must carry a bearer token"
    auth = verify_token(token, expected_audience="genus-bridge")
    assert auth.tenant_id == "tenant-a"
    assert auth.is_service


async def test_an_unbound_process_sends_no_token(service, monkeypatch):
    monkeypatch.delenv("ROBOTHOR_TENANT_ID", raising=False)
    monkeypatch.delenv("ROBOTHOR_DEFAULT_TENANT", raising=False)
    client = _capture_post()
    with patch("httpx.AsyncClient", return_value=client):
        await service.ingest_event("Alice arrived", {"camera": "front"})
    assert "Authorization" not in _sent_headers(client)
    assert "X-Tenant-Id" not in _sent_headers(client)


async def test_vision_never_generates_a_signing_key_to_mint_with(service, monkeypatch):
    """No key provisioned anywhere: post tokenless, and do not create one.

    ``signing_key()`` generates and stores a key on first boot. Done from the
    vision process -- whose unit loads no secrets file -- that would store a key
    the bridge and orchestrator (reading theirs from the environment) do not
    use, and every event would then carry a token they reject.
    """
    from types import SimpleNamespace

    from robothor.auth import tokens

    monkeypatch.setenv("ROBOTHOR_TENANT_ID", "tenant-a")
    monkeypatch.delenv("GENUS_AUTH_SIGNING_KEY", raising=False)
    tokens.reset_signing_key_cache()
    client = _capture_post()
    with (
        patch(
            "robothor.secrets.resolve_secret",
            return_value=SimpleNamespace(value=None, source="missing"),
        ),
        patch("robothor.vault.set") as vault_set,
        patch("httpx.AsyncClient", return_value=client),
    ):
        await service.ingest_event("Alice arrived", {"camera": "front"})
    vault_set.assert_not_called()
    client.post.assert_awaited_once()
    assert "Authorization" not in _sent_headers(client)


async def test_a_process_that_cannot_mint_still_posts_tokenless(service, monkeypatch):
    from robothor.auth.tokens import TokenError

    monkeypatch.setenv("ROBOTHOR_TENANT_ID", "tenant-a")
    client = _capture_post()
    with (
        patch("robothor.auth.tokens.issue_service_token", side_effect=TokenError("no key")),
        patch("httpx.AsyncClient", return_value=client),
    ):
        await service.ingest_event("Alice arrived", {"camera": "front"})
    client.post.assert_awaited_once()
    assert "Authorization" not in _sent_headers(client)
    # No bare tenant header either: it would be refused as an unverified claim.
    assert "X-Tenant-Id" not in _sent_headers(client)
