"""The bridge must say out loud when it cannot complete any sign-in.

``_sso_secret_ok`` reads ``GENUS_BRIDGE_SSO_SECRET`` and returns False when it
is unset. That refusal is correct — fail-closed — but it was *silent*: no log
line, nothing in /health, nothing in /ready. On 2026-09-03 the box rebooted at
02:51, robothor-bridge started twelve seconds later, and the only unit that
decrypts /run/robothor/secrets.env (the engine's ExecStartPre) had not run yet.
``EnvironmentFile=-`` is optional, so the bridge came up without the secret and
answered every ``POST /api/auth/sso`` with 403 for eight days. The refusal is
kept; what is added is that it is now visible.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SSO_SECRET_ENV = "GENUS_BRIDGE_SSO_SECRET"


@pytest.fixture(autouse=True)
def _rearm_alarm():
    """Each test starts from a clean alarm so the once-only log is observable."""
    import routers.auth as auth_router

    auth_router.reset_sso_secret_alarm()
    yield
    auth_router.reset_sso_secret_alarm()


def test_missing_secret_logs_one_error_naming_the_variable(monkeypatch, caplog):
    import routers.auth as auth_router

    monkeypatch.delenv(SSO_SECRET_ENV, raising=False)

    with caplog.at_level(logging.ERROR, logger=auth_router.logger.name):
        assert auth_router.sso_secret_present() is False

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1, f"expected exactly one error, got {[r.getMessage() for r in errors]}"
    message = errors[0].getMessage()
    assert SSO_SECRET_ENV in message
    assert "refused" in message.lower()


def test_the_alarm_message_is_a_literal_with_no_arguments(monkeypatch, caplog):
    """Nothing derived from the secret may reach the logger — not even its
    variable's NAME.

    CodeQL's py/clear-text-logging-sensitive-data classifies an identifier
    containing "SECRET" as sensitive by name, so passing a constant called
    SSO_SECRET_ENV to logger.error failed PR #483 at HIGH severity — despite
    the constant holding only the spelling of the variable, and the branch
    being reachable only when the value is empty. Rather than argue the false
    positive, the logger is only ever handed a literal: a log record with no
    args cannot carry a value, and that is checkable.
    """
    import routers.auth as auth_router

    monkeypatch.delenv(SSO_SECRET_ENV, raising=False)

    with caplog.at_level(logging.ERROR, logger=auth_router.logger.name):
        auth_router.sso_secret_present()

    record = next(r for r in caplog.records if r.levelno >= logging.ERROR)
    assert not record.args, (
        f"the alarm interpolates {record.args!r} — the message must be a single "
        "literal so no expression touching the secret can flow into the logger"
    )
    assert SSO_SECRET_ENV in record.msg


def test_the_alarm_does_not_repeat_on_every_exchange(monkeypatch, caplog):
    import routers.auth as auth_router

    monkeypatch.delenv(SSO_SECRET_ENV, raising=False)

    with caplog.at_level(logging.ERROR, logger=auth_router.logger.name):
        for _ in range(5):
            assert auth_router._sso_secret_ok("anything") is False

    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1


def test_configured_secret_is_silent(monkeypatch, caplog):
    import routers.auth as auth_router

    monkeypatch.setenv(SSO_SECRET_ENV, "dashboard-shared-secret")

    with caplog.at_level(logging.ERROR, logger=auth_router.logger.name):
        assert auth_router.sso_secret_present() is True

    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []


@pytest.mark.asyncio
async def test_readiness_names_the_missing_secret(test_client, mock_http_client, monkeypatch):
    """A bridge that cannot complete any login must not report ready."""
    monkeypatch.delenv(SSO_SECRET_ENV, raising=False)
    monkeypatch.setenv("GENUS_AUTH_ENFORCE", "true")
    mock_http_client.get = AsyncMock(return_value=MagicMock(spec=httpx.Response, status_code=200))

    with patch("robothor.crm.dal.check_health", return_value={"status": "ok"}):
        r = await test_client.get("/ready")

    checks = r.json()["checks"]
    assert SSO_SECRET_ENV in checks["sso_secret"]
    assert checks["sso_secret"].startswith("error:")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_readiness_does_not_fail_a_deployment_that_needs_no_sso(
    test_client, mock_http_client, monkeypatch
):
    """Loopback development legitimately runs with no shared secret.

    Marking such a bridge not-ready forever is not a warning, it is an outage:
    under Helm the readiness probe removes the pod from its Service, so the
    check meant to expose a broken login would take down a deployment that
    never had one. The health contract has only "ok" and "error:…", so "not
    applicable" has to read as ok. The 403 refusal itself is unchanged.
    """
    monkeypatch.delenv(SSO_SECRET_ENV, raising=False)
    monkeypatch.delenv("GENUS_AUTH_ENFORCE", raising=False)
    monkeypatch.setenv("GENUS_INSECURE_DEV_MODE", "true")
    monkeypatch.setenv("ROBOTHOR_BRIDGE_HOST", "127.0.0.1")
    mock_http_client.get = AsyncMock(return_value=MagicMock(spec=httpx.Response, status_code=200))

    with patch("robothor.crm.dal.check_health", return_value={"status": "ok"}):
        r = await test_client.get("/ready")

    assert r.json()["checks"]["sso_secret"] == "ok"
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_readiness_is_ok_when_the_secret_is_configured(
    test_client, mock_http_client, monkeypatch
):
    monkeypatch.setenv(SSO_SECRET_ENV, "dashboard-shared-secret")
    mock_http_client.get = AsyncMock(return_value=MagicMock(spec=httpx.Response, status_code=200))

    with patch("robothor.crm.dal.check_health", return_value={"status": "ok"}):
        r = await test_client.get("/ready")

    assert r.json()["checks"]["sso_secret"] == "ok"
    assert r.status_code == 200
