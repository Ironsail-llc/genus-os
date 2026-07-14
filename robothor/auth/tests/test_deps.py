"""AuthContext + request verification."""

from __future__ import annotations

import pytest

from robothor.auth import deps, tokens
from robothor.auth.tokens import TokenError


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "test-signing-key-at-least-32-bytes-long-xyz")
    tokens.reset_signing_key_cache()
    yield
    tokens.reset_signing_key_cache()


def test_verify_token_builds_context():
    t = tokens.issue_access_token("u1", "tenant-x", "admin")
    ctx = deps.verify_token(t)
    assert ctx.user_id == "u1"
    assert ctx.tenant_id == "tenant-x"
    assert ctx.role == "admin"
    assert ctx.typ == "user"
    assert ctx.audience == "genus-bridge"
    assert ctx.scopes == frozenset({"audit:read", "bridge:*", "engine:*", "tenant:admin"})
    assert ctx.agent_id is None
    assert ctx.token_id
    assert ctx.is_service is False
    assert ctx.actor_id == "u1"
    assert ctx.has_scope("bridge:write") is True


def test_service_context():
    t = tokens.issue_access_token("svc", "t", "", typ="service")
    ctx = deps.verify_token(t)
    assert ctx.is_service is True
    assert ctx.agent_id == "svc"
    assert ctx.actor_id == "svc"


def test_missing_sub_or_tid_raises():
    import jwt

    bad = jwt.encode(
        {"iss": "genus-os", "exp": 9999999999, "sub": "u"},
        "test-signing-key-at-least-32-bytes-long-xyz",
        algorithm="HS256",
    )
    with pytest.raises(TokenError):
        deps.verify_token(bad)


def test_bearer_parsing():
    assert deps.bearer_from_header("Bearer abc.def.ghi") == "abc.def.ghi"
    assert deps.bearer_from_header("bearer xyz") == "xyz"
    assert deps.bearer_from_header("Basic abc") is None
    assert deps.bearer_from_header(None) is None
    assert deps.bearer_from_header("") is None


def test_token_from_request_header_then_cookie():
    class Req:
        headers = {"authorization": "Bearer header-token"}
        cookies = {deps.SESSION_COOKIE: "cookie-token"}

    assert deps.token_from_request(Req()) == "header-token"

    class ReqCookieOnly:
        headers = {}
        cookies = {deps.SESSION_COOKIE: "cookie-token"}

    assert deps.token_from_request(ReqCookieOnly()) == "cookie-token"

    class ReqNeither:
        headers = {}
        cookies = {}

    assert deps.token_from_request(ReqNeither()) is None
