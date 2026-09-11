"""Bridge local email+password endpoints: gating, shapes, throttling, secrecy."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robothor.auth.local_login import LoginResult  # noqa: E402

SIGNING_KEY = "test-signing-key-at-least-32-bytes-long-xyz"

TOKENS = {
    "access_token": "access-1",
    "refresh_token": "refresh-1",
    "user": {
        "id": "uid-1",
        "email": "alice@example.com",
        "display_name": "Alice",
        "role": "owner",
        "tenant_id": "default",
    },
}


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", SIGNING_KEY)
    monkeypatch.setenv("GENUS_BRIDGE_SSO_SECRET", "dashboard-shared-secret")
    monkeypatch.setenv("GENUS_LOCAL_LOGIN", "true")
    monkeypatch.delenv("GENUS_AUTH_ENFORCE", raising=False)
    monkeypatch.setenv("GENUS_OIDC_ISSUERS", "https://idp")
    monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
    from robothor.auth import local_login, tokens

    tokens.reset_signing_key_cache()
    local_login.reset_rate_limiter()
    yield
    tokens.reset_signing_key_cache()
    local_login.reset_rate_limiter()


def _bearer() -> dict[str, str]:
    from robothor.auth import tokens

    return {"Authorization": f"Bearer {tokens.issue_access_token('uid-1', 'default', 'owner')}"}


# ── the feature gate ─────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/auth/login", {"email": "alice@example.com", "password": "x" * 12}),
        ("post", "/api/auth/password", {"current_password": "x" * 12, "new_password": "y" * 12}),
        ("post", "/api/auth/mfa/enroll", {}),
        ("post", "/api/auth/mfa/confirm", {"code": "123456"}),
        ("post", "/api/auth/mfa/disable", {"password": "x" * 12, "code": "123456"}),
    ],
)
async def test_routes_404_when_local_login_is_disabled(
    test_client, monkeypatch, method, path, body
):
    monkeypatch.setenv("GENUS_LOCAL_LOGIN", "false")
    r = await getattr(test_client, method)(path, json=body, headers=_bearer())
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_methods_still_answers_when_local_login_is_disabled(test_client, monkeypatch):
    monkeypatch.setenv("GENUS_LOCAL_LOGIN", "false")
    r = await test_client.get("/api/auth/methods")
    assert r.status_code == 200
    assert r.json()["local"] is False


# ── /methods ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_methods_shape(test_client, monkeypatch):
    monkeypatch.setenv("GENUS_OIDC_ISSUERS", "https://idp.example.test,https://other.example.test")
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "team.example.com")
    monkeypatch.setenv("CF_ACCESS_AUD", "aud-1")
    r = await test_client.get("/api/auth/methods")
    assert r.status_code == 200
    body = r.json()
    assert body["local"] is True
    assert body["oidc"] == ["https://idp.example.test", "https://other.example.test"]
    assert body["cloudflare_access"] is True
    assert set(body) == {"local", "oidc", "cloudflare_access"}


@pytest.mark.asyncio
async def test_methods_never_echoes_a_secret(test_client, monkeypatch):
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "team.example.com")
    monkeypatch.setenv("CF_ACCESS_AUD", "super-secret-audience")
    r = await test_client.get("/api/auth/methods")
    assert "super-secret-audience" not in r.text
    assert "dashboard-shared-secret" not in r.text
    assert SIGNING_KEY not in r.text


@pytest.mark.asyncio
async def test_methods_is_reachable_without_a_token(test_client, monkeypatch):
    """It has to be: the sign-in page asks it which form to render."""
    monkeypatch.setenv("GENUS_AUTH_ENFORCE", "true")
    r = await test_client.get("/api/auth/methods")
    assert r.status_code == 200


# ── /login ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_login_returns_the_same_shape_as_sso(test_client):
    with patch(
        "routers.auth.local_login.authenticate",
        return_value=LoginResult(ok=True, tokens=dict(TOKENS), mfa_setup_required=True, error=""),
    ):
        r = await test_client.post(
            "/api/auth/login", json={"email": "alice@example.com", "password": "x" * 12}
        )
    assert r.status_code == 200
    body = r.json()
    assert body["access_token"] == "access-1"
    assert body["refresh_token"] == "refresh-1"
    assert body["user"] == TOKENS["user"]
    assert body["mfa_setup_required"] is True


@pytest.mark.asyncio
async def test_login_is_reachable_without_a_token_even_when_auth_is_enforced(
    test_client, monkeypatch
):
    monkeypatch.setenv("GENUS_AUTH_ENFORCE", "true")
    with patch("routers.auth.local_login.authenticate", return_value=LoginResult()):
        r = await test_client.post(
            "/api/auth/login", json={"email": "alice@example.com", "password": "x" * 12}
        )
    assert r.status_code == 401
    assert r.json() == {"error": "invalid credentials"}


@pytest.mark.asyncio
async def test_login_failure_is_one_generic_message(test_client):
    with patch("routers.auth.local_login.authenticate", return_value=LoginResult()):
        r = await test_client.post(
            "/api/auth/login", json={"email": "nobody@example.com", "password": "x" * 12}
        )
    assert r.status_code == 401
    assert r.json() == {"error": "invalid credentials"}


@pytest.mark.asyncio
async def test_login_mfa_required(test_client):
    with patch(
        "routers.auth.local_login.authenticate",
        return_value=LoginResult(mfa_required=True, error="mfa_required"),
    ):
        r = await test_client.post(
            "/api/auth/login", json={"email": "alice@example.com", "password": "x" * 12}
        )
    assert r.status_code == 401
    assert r.json() == {"error": "mfa_required"}


@pytest.mark.asyncio
async def test_sixth_attempt_in_a_minute_is_429(test_client):
    account = {
        "id": "uid-1",
        "tenant_id": "default",
        "email": "alice@example.com",
        "display_name": "Alice",
        "role": "member",
        "status": "active",
        "password_hash": None,
        "mfa_enabled": False,
        "mfa_secret_enc": None,
        "failed_login_count": 0,
        "locked_until": None,
    }
    with (
        patch("robothor.auth.local_login._load_account", return_value=account),
        patch("robothor.auth.local_login._record_failure"),
    ):
        for _ in range(5):
            r = await test_client.post(
                "/api/auth/login", json={"email": "alice@example.com", "password": "x" * 12}
            )
            assert r.status_code == 401
        r = await test_client.post(
            "/api/auth/login", json={"email": "alice@example.com", "password": "x" * 12}
        )
    assert r.status_code == 429
    assert "password" not in r.text


@pytest.mark.asyncio
async def test_login_never_echoes_the_password(test_client):
    with patch("routers.auth.local_login.authenticate", return_value=LoginResult()):
        r = await test_client.post(
            "/api/auth/login",
            json={"email": "alice@example.com", "password": "hunter2-hunter2"},
        )
    assert "hunter2" not in r.text


@pytest.mark.asyncio
async def test_login_rejects_an_oversized_password_without_hashing(test_client):
    with patch("routers.auth.local_login.authenticate") as auth:
        r = await test_client.post(
            "/api/auth/login", json={"email": "alice@example.com", "password": "x" * 100_000}
        )
    assert r.status_code in (401, 422)
    auth.assert_not_called()


@pytest.mark.asyncio
async def test_login_rejects_unknown_fields(test_client):
    r = await test_client.post(
        "/api/auth/login",
        json={"email": "a@example.com", "password": "x" * 12, "tenant_id": "other"},
    )
    assert r.status_code == 422


# ── /password ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_password_change_requires_authentication(test_client, monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_ENFORCE", "true")
    r = await test_client.post(
        "/api/auth/password", json={"current_password": "x" * 12, "new_password": "y" * 12}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_password_change_refuses_a_wrong_current_password(test_client):
    with patch("routers.auth.local_login.change_password", return_value=False):
        r = await test_client.post(
            "/api/auth/password",
            json={"current_password": "wrong-enough", "new_password": "y" * 12},
            headers=_bearer(),
        )
    assert r.status_code == 401
    assert r.json() == {"error": "invalid credentials"}


@pytest.mark.asyncio
async def test_password_change_enforces_a_minimum_length(test_client):
    with patch("routers.auth.local_login.change_password") as change:
        r = await test_client.post(
            "/api/auth/password",
            json={"current_password": "x" * 12, "new_password": "short"},
            headers=_bearer(),
        )
    assert r.status_code == 422
    change.assert_not_called()


@pytest.mark.asyncio
async def test_password_change_succeeds(test_client):
    with patch("routers.auth.local_login.change_password", return_value=True) as change:
        r = await test_client.post(
            "/api/auth/password",
            json={"current_password": "x" * 12, "new_password": "y" * 14},
            headers=_bearer(),
        )
    assert r.status_code == 200 and r.json() == {"success": True}
    assert change.call_args[0][0] == "uid-1"


# ── MFA enroll / confirm / disable ───────────────────────────────────


@pytest.mark.asyncio
async def test_enroll_returns_the_uri_and_secret_once(test_client):
    payload = {"secret": "JBSWY3DPEHPK3PXP", "otpauth_uri": "otpauth://totp/x"}
    with (
        patch("routers.auth.local_login.begin_enrollment", return_value=payload) as begin,
        patch(
            "routers.auth.accounts.get_account_by_id",
            return_value={
                "id": "uid-1",
                "email": "alice@example.com",
                "status": "active",
                "display_name": "Alice",
                "role": "owner",
                "tenant_id": "default",
            },
        ),
    ):
        r = await test_client.post("/api/auth/mfa/enroll", json={}, headers=_bearer())
    assert r.status_code == 200
    assert r.json() == payload
    assert begin.call_args[0][0] == "uid-1"


@pytest.mark.asyncio
async def test_enroll_refuses_to_replace_a_live_factor(test_client):
    """A hijacked session must not be able to turn the victim's second factor
    off by re-enrolling; disabling deliberately costs a password AND a code."""
    from robothor.auth.local_login import MfaAlreadyEnabledError

    with (
        patch(
            "routers.auth.local_login.begin_enrollment",
            side_effect=MfaAlreadyEnabledError("already on"),
        ),
        patch(
            "routers.auth.accounts.get_account_by_id",
            return_value={
                "id": "uid-1",
                "email": "alice@example.com",
                "status": "active",
                "display_name": "Alice",
                "role": "owner",
                "tenant_id": "default",
                "mfa_enabled": True,
            },
        ),
    ):
        r = await test_client.post("/api/auth/mfa/enroll", json={}, headers=_bearer())
    assert r.status_code == 409
    assert "secret" not in r.text


@pytest.mark.asyncio
async def test_enroll_requires_authentication(test_client, monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_ENFORCE", "true")
    r = await test_client.post("/api/auth/mfa/enroll", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_confirm_enables_and_a_wrong_code_does_not(test_client):
    with patch("routers.auth.local_login.confirm_enrollment", return_value=True):
        r = await test_client.post(
            "/api/auth/mfa/confirm", json={"code": "123456"}, headers=_bearer()
        )
    assert r.status_code == 200 and r.json() == {"success": True}

    with patch("routers.auth.local_login.confirm_enrollment", return_value=False):
        r = await test_client.post(
            "/api/auth/mfa/confirm", json={"code": "000000"}, headers=_bearer()
        )
    assert r.status_code == 401 and r.json() == {"error": "invalid credentials"}


@pytest.mark.asyncio
async def test_disable_requires_password_and_code(test_client):
    with patch("routers.auth.local_login.disable_mfa", return_value=False):
        r = await test_client.post(
            "/api/auth/mfa/disable",
            json={"password": "x" * 12, "code": "000000"},
            headers=_bearer(),
        )
    assert r.status_code == 401
    with patch("routers.auth.local_login.disable_mfa", return_value=True):
        r = await test_client.post(
            "/api/auth/mfa/disable",
            json={"password": "x" * 12, "code": "123456"},
            headers=_bearer(),
        )
    assert r.status_code == 200 and r.json() == {"success": True}


@pytest.mark.asyncio
async def test_mfa_routes_are_throttled_too(test_client):
    """Confirm/disable take a guessable six-digit code from an authenticated
    but possibly hijacked session; unlimited guesses would defeat the factor."""
    with patch("routers.auth.local_login.confirm_enrollment", return_value=False):
        statuses = []
        for _ in range(7):
            r = await test_client.post(
                "/api/auth/mfa/confirm", json={"code": "000000"}, headers=_bearer()
            )
            statuses.append(r.status_code)
    assert 429 in statuses


# ── /me ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_me_reports_mfa_setup_required(test_client, monkeypatch):
    monkeypatch.delenv("GENUS_OIDC_ISSUERS", raising=False)
    account = {
        "id": "uid-1",
        "email": "alice@example.com",
        "display_name": "Alice",
        "role": "owner",
        "tenant_id": "default",
        "status": "active",
        "mfa_enabled": False,
    }
    with patch("routers.auth.accounts.get_account_by_id", return_value=account):
        r = await test_client.get("/api/auth/me", headers=_bearer())
    assert r.status_code == 200
    body = r.json()
    assert body["mfa_setup_required"] is True
    assert body["mfa_enabled"] is False
    assert "password_hash" not in body and "mfa_secret_enc" not in body


# ── a rejected body must never echo the credential ───────────────────
#
# FastAPI serializes pydantic's ``input`` into the 422 body, and for a
# ``missing`` error that input is the WHOLE request body. A pydantic
# LoginRequest therefore answered {"password": "hunter2"} (no email) with the
# password in the response — into the browser, the access log, and every proxy
# in between. These routes parse their own bodies for exactly that reason.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"password": "hunter2-hunter2"},
        {"email": "alice@example.com", "password": "hunter2-hunter2", "extra": "x"},
        {"email": "alice@example.com", "password": "hunter2-hunter2" * 500},
        {"email": "alice@example.com", "password": "hunter2-hunter2", "code": "0" * 64},
    ],
)
async def test_a_rejected_login_body_never_echoes_the_password(test_client, body):
    r = await test_client.post("/api/auth/login", json=body)
    assert r.status_code in (401, 422)
    assert "hunter2" not in r.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"new_password": "hunter2-hunter2"},
        {"current_password": "hunter2-hunter2"},
        {"current_password": "hunter2-hunter2", "new_password": "short", "extra": 1},
    ],
)
async def test_a_rejected_password_change_never_echoes_the_password(test_client, body):
    r = await test_client.post("/api/auth/password", json=body, headers=_bearer())
    assert r.status_code in (401, 422)
    assert "hunter2" not in r.text


@pytest.mark.asyncio
async def test_a_rejected_mfa_disable_never_echoes_the_password(test_client):
    r = await test_client.post("/api/auth/mfa/disable", json={"code": "123456"}, headers=_bearer())
    assert r.status_code in (401, 422)
    r = await test_client.post(
        "/api/auth/mfa/disable", json={"password": "hunter2-hunter2"}, headers=_bearer()
    )
    assert "hunter2" not in r.text


@pytest.mark.asyncio
async def test_me_does_not_force_mfa_when_oidc_exists(test_client, monkeypatch):
    monkeypatch.setenv("GENUS_OIDC_ISSUERS", "https://idp.example.test")
    account = {
        "id": "uid-1",
        "email": "alice@example.com",
        "display_name": "Alice",
        "role": "owner",
        "tenant_id": "default",
        "status": "active",
        "mfa_enabled": False,
    }
    with patch("routers.auth.accounts.get_account_by_id", return_value=account):
        r = await test_client.get("/api/auth/me", headers=_bearer())
    assert r.json()["mfa_setup_required"] is False
