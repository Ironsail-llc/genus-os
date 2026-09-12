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
        ("post", "/api/auth/mfa/enroll", {"password": "x" * 12}),
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


# ── client IP: trusted proxy only ────────────────────────────────────
#
# The dashboard calls the bridge SERVER-side, so request.client.host is the
# dashboard pod for every sign-in on the planet. Left like that the limiter's
# IP dimension is inert — one bucket for the whole internet — which is both a
# free denial-of-service on any account (five attempts locks everyone out of
# that email) and no protection at all against a distributed spray.


@pytest.mark.asyncio
async def test_loopback_is_not_trusted_implicitly(test_client, monkeypatch):
    """cloudflared, a reverse proxy or any tunnel on the same host makes every
    remote client a loopback peer. Trusting loopback by default therefore lets
    every one of them pick its own rate-limit bucket by sending a header —
    which is the whole limiter, gone, on the strength of a deployment detail."""
    monkeypatch.delenv("GENUS_TRUSTED_PROXIES", raising=False)
    with (
        patch("routers.auth.local_login.authenticate", return_value=LoginResult()) as auth,
        patch("routers.auth._peer_ip", return_value="127.0.0.1"),
    ):
        await test_client.post(
            "/api/auth/login",
            json={"email": "alice@example.com", "password": "x" * 12},
            headers={"X-Client-IP": "203.0.113.7"},
        )
    assert auth.call_args.kwargs["ip"] == "127.0.0.1"


@pytest.mark.asyncio
async def test_loopback_is_trusted_when_listed_explicitly(test_client, monkeypatch):
    monkeypatch.setenv("GENUS_TRUSTED_PROXIES", "127.0.0.1/32")
    with (
        patch("routers.auth.local_login.authenticate", return_value=LoginResult()) as auth,
        patch("routers.auth._peer_ip", return_value="127.0.0.1"),
    ):
        await test_client.post(
            "/api/auth/login",
            json={"email": "alice@example.com", "password": "x" * 12},
            headers={"X-Client-IP": "203.0.113.7"},
        )
    assert auth.call_args.kwargs["ip"] == "203.0.113.7"


@pytest.mark.asyncio
async def test_a_non_empty_allowlist_that_omits_loopback_still_excludes_it(
    test_client, monkeypatch
):
    monkeypatch.setenv("GENUS_TRUSTED_PROXIES", "10.42.0.0/16")
    with (
        patch("routers.auth.local_login.authenticate", return_value=LoginResult()) as auth,
        patch("routers.auth._peer_ip", return_value="127.0.0.1"),
    ):
        await test_client.post(
            "/api/auth/login",
            json={"email": "alice@example.com", "password": "x" * 12},
            headers={"X-Client-IP": "203.0.113.7"},
        )
    assert auth.call_args.kwargs["ip"] == "127.0.0.1"


@pytest.mark.asyncio
async def test_the_forwarded_client_ip_is_ignored_from_an_untrusted_caller(
    test_client, monkeypatch
):
    """Otherwise anyone who can reach the bridge picks their own limiter bucket
    by sending a fresh header value on every attempt — an unlimited spray."""
    monkeypatch.setenv("GENUS_TRUSTED_PROXIES", "10.9.9.9")
    with (
        patch("routers.auth.local_login.authenticate", return_value=LoginResult()) as auth,
        patch("routers.auth._peer_ip", return_value="198.51.100.4"),
    ):
        await test_client.post(
            "/api/auth/login",
            json={"email": "alice@example.com", "password": "x" * 12},
            headers={"X-Client-IP": "203.0.113.7"},
        )
    assert auth.call_args.kwargs["ip"] == "198.51.100.4"


@pytest.mark.asyncio
async def test_a_listed_proxy_is_trusted(test_client, monkeypatch):
    monkeypatch.setenv("GENUS_TRUSTED_PROXIES", "198.51.100.4, 10.9.9.9")
    with (
        patch("routers.auth.local_login.authenticate", return_value=LoginResult()) as auth,
        patch("routers.auth._peer_ip", return_value="198.51.100.4"),
    ):
        await test_client.post(
            "/api/auth/login",
            json={"email": "alice@example.com", "password": "x" * 12},
            headers={"X-Client-IP": "203.0.113.7"},
        )
    assert auth.call_args.kwargs["ip"] == "203.0.113.7"


@pytest.mark.asyncio
async def test_a_proxy_inside_a_trusted_cidr_range_is_trusted(test_client, monkeypatch):
    """A Kubernetes pod CIDR is the realistic way to express "the dashboard",
    because the pod's address changes on every restart. Documenting CIDR in
    robothor.env.example while matching only exact strings would be a config
    that silently does nothing."""
    monkeypatch.setenv("GENUS_TRUSTED_PROXIES", "10.42.0.0/16")
    with (
        patch("routers.auth.local_login.authenticate", return_value=LoginResult()) as auth,
        patch("routers.auth._peer_ip", return_value="10.42.7.31"),
    ):
        await test_client.post(
            "/api/auth/login",
            json={"email": "alice@example.com", "password": "x" * 12},
            headers={"X-Client-IP": "203.0.113.7"},
        )
    assert auth.call_args.kwargs["ip"] == "203.0.113.7"


@pytest.mark.asyncio
async def test_a_peer_outside_the_trusted_cidr_range_is_not(test_client, monkeypatch):
    monkeypatch.setenv("GENUS_TRUSTED_PROXIES", "10.42.0.0/16")
    with (
        patch("routers.auth.local_login.authenticate", return_value=LoginResult()) as auth,
        patch("routers.auth._peer_ip", return_value="10.43.7.31"),
    ):
        await test_client.post(
            "/api/auth/login",
            json={"email": "alice@example.com", "password": "x" * 12},
            headers={"X-Client-IP": "203.0.113.7"},
        )
    assert auth.call_args.kwargs["ip"] == "10.43.7.31"


@pytest.mark.asyncio
async def test_a_malformed_trusted_proxies_entry_is_ignored_not_fatal(test_client, monkeypatch):
    """A typo in an env var must not turn every sign-in into a 500."""
    monkeypatch.setenv("GENUS_TRUSTED_PROXIES", "not-a-range, 10.42.0.0/16")
    with (
        patch("routers.auth.local_login.authenticate", return_value=LoginResult()) as auth,
        patch("routers.auth._peer_ip", return_value="10.42.7.31"),
    ):
        r = await test_client.post(
            "/api/auth/login",
            json={"email": "alice@example.com", "password": "x" * 12},
            headers={"X-Client-IP": "203.0.113.7"},
        )
    assert r.status_code == 401
    assert auth.call_args.kwargs["ip"] == "203.0.113.7"


@pytest.mark.asyncio
async def test_a_junk_forwarded_value_falls_back_to_the_peer(test_client, monkeypatch):
    monkeypatch.setenv("GENUS_TRUSTED_PROXIES", "127.0.0.1")
    with (
        patch("routers.auth.local_login.authenticate", return_value=LoginResult()) as auth,
        patch("routers.auth._peer_ip", return_value="127.0.0.1"),
    ):
        await test_client.post(
            "/api/auth/login",
            json={"email": "alice@example.com", "password": "x" * 12},
            headers={"X-Client-IP": "not-an-address"},
        )
    assert auth.call_args.kwargs["ip"] == "127.0.0.1"


# ── email shape ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "email",
    ["alice", "alice@", "@example.com", "alice@@example.com", "a@b@example.com", "alice@ "],
)
async def test_a_malformed_email_is_rejected_before_any_hashing(test_client, email):
    with patch("routers.auth.local_login.authenticate") as auth:
        r = await test_client.post("/api/auth/login", json={"email": email, "password": "x" * 12})
    assert r.status_code == 422
    auth.assert_not_called()


@pytest.mark.asyncio
async def test_a_well_formed_email_still_gets_through(test_client):
    with patch("routers.auth.local_login.authenticate", return_value=LoginResult()) as auth:
        r = await test_client.post(
            "/api/auth/login", json={"email": "a.b+tag@example.com", "password": "x" * 12}
        )
    assert r.status_code == 401
    auth.assert_called_once()


# ── failed-login audit carries a subject, never the address ──────────


@pytest.mark.asyncio
async def test_a_failed_login_is_audited_with_a_hashed_subject_and_the_ip(test_client, monkeypatch):
    monkeypatch.setenv("GENUS_TRUSTED_PROXIES", "127.0.0.1")
    with (
        patch("routers.auth.local_login.authenticate", return_value=LoginResult()),
        patch("routers.auth.audited") as audit,
    ):
        await test_client.post(
            "/api/auth/login",
            json={"email": "Alice@Example.com", "password": "x" * 12},
            headers={"X-Client-IP": "203.0.113.7"},
        )
    kwargs = audit.call_args.kwargs
    assert kwargs["ip"] == "203.0.113.7"
    subject = kwargs["subject"]
    assert subject and subject != "unavailable"
    blob = str(audit.call_args)
    assert "Alice@Example.com" not in blob
    assert "alice@example.com" not in blob


@pytest.mark.asyncio
async def test_the_audit_subject_is_stable_across_case(test_client):
    seen = []
    for address in ("Alice@Example.com", "alice@example.com", "ALICE@EXAMPLE.COM"):
        with (
            patch("routers.auth.local_login.authenticate", return_value=LoginResult()),
            patch("routers.auth.audited") as audit,
        ):
            await test_client.post("/api/auth/login", json={"email": address, "password": "x" * 12})
        seen.append(audit.call_args.kwargs["subject"])
    assert len(set(seen)) == 1, "a spray against one account must look like one account"


@pytest.mark.asyncio
async def test_password_change_spares_the_callers_own_session(test_client):
    """Revoking every session would throw the operator out of the panel they
    are standing in, which trains people not to change their password."""
    with patch("routers.auth.local_login.change_password", return_value=True) as change:
        r = await test_client.post(
            "/api/auth/password",
            json={
                "current_password": "x" * 12,
                "new_password": "y" * 14,
                "keep_refresh_token": "raw-refresh-token",
            },
            headers=_bearer(),
        )
    assert r.status_code == 200

    from robothor.auth import tokens

    assert change.call_args.kwargs["keep_refresh_hash"] == tokens.hash_refresh_token(
        "raw-refresh-token"
    )


@pytest.mark.asyncio
async def test_an_oversized_kept_token_is_refused_before_any_hashing(test_client):
    """Every other field in this body is bounded; an unbounded one is a free
    megabyte of SHA-256 for a caller who has a session and nothing else."""
    with patch("routers.auth.local_login.change_password") as change:
        r = await test_client.post(
            "/api/auth/password",
            json={
                "current_password": "x" * 12,
                "new_password": "y" * 14,
                "keep_refresh_token": "z" * 100_000,
            },
            headers=_bearer(),
        )
    assert r.status_code == 422
    change.assert_not_called()


@pytest.mark.asyncio
async def test_password_change_without_a_kept_token_revokes_everything(test_client):
    with patch("routers.auth.local_login.change_password", return_value=True) as change:
        await test_client.post(
            "/api/auth/password",
            json={"current_password": "x" * 12, "new_password": "y" * 14},
            headers=_bearer(),
        )
    assert change.call_args.kwargs["keep_refresh_hash"] is None


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/auth/password", {"current_password": "x" * 12, "new_password": "y" * 14}),
        ("/api/auth/mfa/enroll", {"password": "x" * 12}),
        ("/api/auth/mfa/confirm", {"code": "123456"}),
        ("/api/auth/mfa/disable", {"password": "x" * 12, "code": "123456"}),
    ],
)
async def test_a_service_token_cannot_touch_human_credentials(test_client, path, body):
    """A service principal has no password and no authenticator to manage.

    Two independent controls refuse it, which is the point: the capabilities
    manifest (instance-owned, and its ``default_policy`` may be ``allow``), and
    the handler's own check — pinned separately below, against the router
    mounted WITHOUT the middleware, so that test cannot pass on the middleware's
    refusal and certify a guard it never reached.
    """
    from robothor.auth import tokens

    token = tokens.issue_service_token(
        "email-classifier", "default", agent_id="email-classifier", scopes=["*"]
    )
    r = await test_client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/auth/password", {"current_password": "x" * 12, "new_password": "y" * 14}),
        ("/api/auth/mfa/enroll", {"password": "x" * 12}),
        ("/api/auth/mfa/confirm", {"code": "123456"}),
        ("/api/auth/mfa/disable", {"password": "x" * 12, "code": "123456"}),
    ],
)
def test_the_handler_itself_refuses_a_service_token(path, body):
    """The middleware is not in this app, so only the route's own guard can
    produce this refusal."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers.auth import router

    from robothor.auth import tokens

    bare = FastAPI()
    bare.include_router(router)
    token = tokens.issue_service_token("svc-1", "default", agent_id="svc-1", scopes=["*"])
    with TestClient(bare) as client:
        r = client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    assert r.json() == {"error": "service credentials cannot manage human credentials"}


# ── MFA enroll / confirm / disable ───────────────────────────────────


@pytest.mark.asyncio
async def test_enroll_returns_the_uri_and_secret_once(test_client):
    payload = {"secret": "JBSWY3DPEHPK3PXP", "otpauth_uri": "otpauth://totp/x"}
    with patch("routers.auth.local_login.begin_enrollment", return_value=payload) as begin:
        r = await test_client.post(
            "/api/auth/mfa/enroll", json={"password": "x" * 12}, headers=_bearer()
        )
    assert r.status_code == 200
    assert r.json() == payload
    assert begin.call_args[0][0] == "uid-1"


@pytest.mark.asyncio
async def test_enroll_requires_the_current_password(test_client):
    """Binding a second factor is a change of authority. A stolen session
    alone must not let an attacker enrol their own authenticator — after which
    the real owner's password is no longer enough to get back in."""
    with patch("routers.auth.local_login.begin_enrollment") as begin:
        r = await test_client.post("/api/auth/mfa/enroll", json={}, headers=_bearer())
    assert r.status_code == 422
    begin.assert_not_called()


@pytest.mark.asyncio
async def test_enroll_refuses_a_wrong_password(test_client):
    from robothor.auth.local_login import EnrollmentDeniedError

    with patch(
        "routers.auth.local_login.begin_enrollment",
        side_effect=EnrollmentDeniedError("nope"),
    ):
        r = await test_client.post(
            "/api/auth/mfa/enroll", json={"password": "wrong-enough"}, headers=_bearer()
        )
    assert r.status_code == 401
    assert r.json() == {"error": "invalid credentials"}


@pytest.mark.asyncio
async def test_enroll_never_echoes_the_password(test_client):
    r = await test_client.post(
        "/api/auth/mfa/enroll", json={"password": "hunter2-hunter2", "x": 1}, headers=_bearer()
    )
    assert "hunter2" not in r.text


@pytest.mark.asyncio
async def test_enroll_is_throttled(test_client):
    """It takes the account password, so unlimited attempts against it are an
    unlimited password-guessing oracle behind one stolen cookie."""
    from robothor.auth.local_login import EnrollmentDeniedError

    statuses = []
    with patch(
        "routers.auth.local_login.begin_enrollment",
        side_effect=EnrollmentDeniedError("nope"),
    ):
        for _ in range(7):
            r = await test_client.post(
                "/api/auth/mfa/enroll", json={"password": "x" * 12}, headers=_bearer()
            )
            statuses.append(r.status_code)
    assert 429 in statuses


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
        r = await test_client.post(
            "/api/auth/mfa/enroll", json={"password": "x" * 12}, headers=_bearer()
        )
    assert r.status_code == 409
    assert "secret" not in r.text


@pytest.mark.asyncio
async def test_enroll_requires_authentication(test_client, monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_ENFORCE", "true")
    r = await test_client.post("/api/auth/mfa/enroll", json={"password": "x" * 12})
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


def _owner_row() -> dict[str, object]:
    return {
        "id": "uid-1",
        "email": "alice@example.com",
        "display_name": "Alice",
        "role": "owner",
        "tenant_id": "default",
        "status": "active",
        "mfa_enabled": False,
    }


@pytest.mark.asyncio
async def test_me_still_forces_owner_mfa_when_oidc_issuers_exist(test_client, monkeypatch):
    """A bridge issuer allowlist is not a sign-in method a human can use.

    The autouse fixture already sets GENUS_OIDC_ISSUERS, which used to turn the
    policy off — so the owner was never told to enrol, and one password was the
    whole authentication for the appliance.
    """
    monkeypatch.setenv("GENUS_OIDC_ISSUERS", "https://idp.example.test")
    with patch("routers.auth.accounts.get_account_by_id", return_value=_owner_row()):
        r = await test_client.get("/api/auth/me", headers=_bearer())
    assert r.json()["mfa_setup_required"] is True


@pytest.mark.asyncio
async def test_me_honours_the_owner_mfa_escape_hatch(test_client, monkeypatch):
    monkeypatch.setenv("GENUS_OWNER_MFA_REQUIRED", "false")
    with patch("routers.auth.accounts.get_account_by_id", return_value=_owner_row()):
        r = await test_client.get("/api/auth/me", headers=_bearer())
    assert r.json()["mfa_setup_required"] is False
