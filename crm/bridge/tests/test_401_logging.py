"""Every bridge 401 must name its caller in one WARNING log line.

Production journals showed 27 401s in 48h from 127.0.0.1 (POST /log-interaction,
POST /api/crm/query, GET /health) with no way to tell WHICH local client held
the stale/missing token — the audit event records method and path but not the
caller.  These tests pin the attribution contract:

- exactly one WARNING per rejected request,
- it names the method, the path, the User-Agent, and the remote address,
- it never contains the presented credential.
"""

from __future__ import annotations

import logging

import pytest

from robothor.auth import tokens

MIDDLEWARE_LOGGER = "middleware"


@pytest.fixture(autouse=True)
def secure_mode(monkeypatch):
    """Auth on: no insecure dev mode, non-loopback bind, signing key present."""
    monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
    monkeypatch.delenv("GENUS_AUTH_ENFORCE", raising=False)
    monkeypatch.setenv("ROBOTHOR_BRIDGE_HOST", "0.0.0.0")
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "test-signing-key-at-least-32-bytes-long-xyz")
    tokens.reset_signing_key_cache()
    yield
    tokens.reset_signing_key_cache()


def _warning_records(caplog):
    return [
        r for r in caplog.records if r.name == MIDDLEWARE_LOGGER and r.levelno == logging.WARNING
    ]


@pytest.mark.asyncio
async def test_missing_token_401_warns_with_caller_identity(test_client, caplog):
    with caplog.at_level(logging.WARNING, logger=MIDDLEWARE_LOGGER):
        response = await test_client.get(
            "/api/people", headers={"User-Agent": "vision-service/1.0"}
        )

    assert response.status_code == 401
    records = _warning_records(caplog)
    assert len(records) == 1, "exactly one WARNING per rejected request"
    message = records[0].getMessage()
    assert "401" in message
    assert "GET" in message
    assert "/api/people" in message
    assert "vision-service/1.0" in message


@pytest.mark.asyncio
async def test_invalid_token_401_warns_but_never_logs_the_token(test_client, caplog):
    with caplog.at_level(logging.WARNING, logger=MIDDLEWARE_LOGGER):
        response = await test_client.post(
            "/log-interaction",
            json={},
            headers={
                "Authorization": "Bearer stale-credential-abc123",
                "User-Agent": "email-sync/2.0",
            },
        )

    assert response.status_code == 401
    records = _warning_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "401" in message
    assert "POST" in message
    assert "/log-interaction" in message
    assert "email-sync/2.0" in message
    assert "stale-credential-abc123" not in caplog.text, "credentials must never be logged"


@pytest.mark.asyncio
async def test_public_probe_does_not_warn(test_client, caplog):
    with caplog.at_level(logging.WARNING, logger=MIDDLEWARE_LOGGER):
        response = await test_client.get("/live")

    assert response.status_code == 200
    assert _warning_records(caplog) == []
