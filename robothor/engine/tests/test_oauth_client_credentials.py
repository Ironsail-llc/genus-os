"""OAuth 2.0 client_credentials token source for http MCP adapters.

Faked at the httpx transport seam (``httpx.MockTransport``), so the real
request the source builds is what the assertions read.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qs

import httpx
import pytest

from robothor.engine.oauth_client_credentials import (
    REFRESH_SKEW_SECONDS,
    OAuthClientCredentials,
    OAuthClientCredentialsTokenSource,
    OAuthTokenError,
)

TOKEN_URL = "https://auth.example.com/oauth/token"
SECRET = "s3cr3t-value-never-logged"


def _creds(scope: str = "platform:read platform:write") -> OAuthClientCredentials:
    return OAuthClientCredentials(
        token_url=TOKEN_URL, client_id="client-123", client_secret=SECRET, scope=scope
    )


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _token_server(
    tokens: list[str], *, expires_in: int = 3600, status: int = 200, body: dict | None = None
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if status != 200:
            return httpx.Response(status, json=body or {})
        return httpx.Response(
            200,
            json={
                "access_token": tokens[len(seen) - 1],
                "token_type": "Bearer",
                "expires_in": expires_in,
            },
        )

    return httpx.MockTransport(handler), seen


class TestMint:
    async def test_posts_client_credentials_grant_to_the_token_url(self):
        transport, seen = _token_server(["tok-1"])
        source = OAuthClientCredentialsTokenSource(_creds(), transport=transport)

        assert await source.token() == "tok-1"

        assert len(seen) == 1
        request = seen[0]
        assert str(request.url) == TOKEN_URL
        assert request.method == "POST"
        form = parse_qs(request.content.decode())
        assert form == {
            "grant_type": ["client_credentials"],
            "client_id": ["client-123"],
            "client_secret": [SECRET],
            "scope": ["platform:read platform:write"],
        }

    async def test_scope_is_omitted_when_not_configured(self):
        transport, seen = _token_server(["tok-1"])
        source = OAuthClientCredentialsTokenSource(_creds(scope=""), transport=transport)

        await source.token()

        assert "scope" not in parse_qs(seen[0].content.decode())

    async def test_refusal_carries_the_oauth_error_code_only(self):
        transport, _ = _token_server(
            [],
            status=401,
            body={"error": "invalid_client", "error_description": f"bad secret {SECRET}"},
        )
        source = OAuthClientCredentialsTokenSource(_creds(), transport=transport)

        with pytest.raises(OAuthTokenError) as info:
            await source.token()

        assert "invalid_client" in str(info.value)
        assert SECRET not in str(info.value)

    async def test_200_without_access_token_is_an_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"token_type": "Bearer"})

        source = OAuthClientCredentialsTokenSource(_creds(), transport=httpx.MockTransport(handler))
        with pytest.raises(OAuthTokenError):
            await source.token()

    async def test_transport_failure_is_an_error_without_the_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        source = OAuthClientCredentialsTokenSource(_creds(), transport=httpx.MockTransport(handler))
        with pytest.raises(OAuthTokenError) as info:
            await source.token()
        assert info.value.__cause__ is None
        assert SECRET not in str(info.value)

    async def test_never_logs_the_secret_or_the_token(self, caplog):
        transport, _ = _token_server(["tok-secret-value"])
        source = OAuthClientCredentialsTokenSource(_creds(), transport=transport)

        with caplog.at_level(logging.DEBUG):
            await source.token()

        text = caplog.text
        assert SECRET not in text
        assert "tok-secret-value" not in text


class TestCache:
    async def test_reuses_the_token_until_expiry_minus_skew(self):
        transport, seen = _token_server(["tok-1", "tok-2"], expires_in=3600)
        clock = _Clock()
        source = OAuthClientCredentialsTokenSource(_creds(), transport=transport, clock=clock)

        assert await source.token() == "tok-1"
        clock.now += 3600 - REFRESH_SKEW_SECONDS - 1
        assert await source.token() == "tok-1"
        assert len(seen) == 1

        clock.now += 2
        assert await source.token() == "tok-2"
        assert len(seen) == 2

    def test_skew_is_sixty_seconds(self):
        assert REFRESH_SKEW_SECONDS == 60.0

    async def test_forget_forces_a_new_mint(self):
        transport, seen = _token_server(["tok-1", "tok-2"])
        source = OAuthClientCredentialsTokenSource(_creds(), transport=transport)

        assert await source.token() == "tok-1"
        source.forget()
        assert await source.token() == "tok-2"
        assert len(seen) == 2

    async def test_token_without_lifetime_is_not_cached(self):
        transport, seen = _token_server(["tok-1", "tok-2"], expires_in=0)
        source = OAuthClientCredentialsTokenSource(_creds(), transport=transport)

        await source.token()
        await source.token()
        assert len(seen) == 2


class TestRepr:
    def test_credentials_repr_hides_the_secret(self):
        assert SECRET not in repr(_creds())
