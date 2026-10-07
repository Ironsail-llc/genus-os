"""Entra app-only token acquisition for the Microsoft 365 transport."""

from __future__ import annotations

import asyncio
import base64
import hashlib

import pytest

from robothor.workspace.errors import AuthError, WorkspaceError
from robothor.workspace.microsoft.auth import (
    ASSERTION_TYPE,
    REFRESH_SKEW_SECONDS,
    ClientCredentialTokenSource,
    token_url,
)
from robothor.workspace.tests.fake_graph import (
    CLIENT_ID,
    TENANT_ID,
    FakeGraphTenant,
    make_certificate,
)


@pytest.fixture(scope="module")
def certificate() -> tuple[str, str]:
    return make_certificate()


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _source(tenant: FakeGraphTenant, **kwargs):
    kwargs.setdefault("transport", tenant.transport())
    return ClientCredentialTokenSource(TENANT_ID, CLIENT_ID, **kwargs)


# ── certificate assertion ─────────────────────────────────────────────────


async def test_certificate_assertion_is_signed_and_shaped(certificate) -> None:
    cert_pem, key_pem = certificate
    tenant = FakeGraphTenant(certificate_pem=cert_pem)
    source = _source(tenant, certificate_pem=cert_pem, private_key_pem=key_pem)

    token = await source.token()

    assert token in tenant.issued_tokens
    form = tenant.token_requests[-1]
    assert form["client_assertion_type"] == ASSERTION_TYPE
    assert "client_secret" not in form
    assert form["scope"] == "https://graph.microsoft.com/.default"
    # The fake verified the signature with the certificate's public key, the
    # audience (the token endpoint) and the issuer before issuing.
    claims, header = tenant.assertions[-1]
    assert claims["aud"] == token_url(TENANT_ID)
    assert claims["iss"] == claims["sub"] == CLIENT_ID
    assert claims["jti"]
    assert claims["exp"] - claims["iat"] == 600
    assert claims["nbf"] <= claims["iat"]
    assert header["alg"] == "RS256"
    assert header["typ"] == "JWT"

    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    der = x509.load_pem_x509_certificate(cert_pem.encode()).public_bytes(serialization.Encoding.DER)
    expected = base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode()
    assert header["x5t#S256"] == expected


async def test_every_assertion_has_a_fresh_jti(certificate) -> None:
    cert_pem, key_pem = certificate
    tenant = FakeGraphTenant(certificate_pem=cert_pem)
    clock = Clock()
    source = _source(tenant, certificate_pem=cert_pem, private_key_pem=key_pem, clock=clock)
    await source.token()
    clock.now += 3600
    await source.token()
    jtis = {claims["jti"] for claims, _ in tenant.assertions}
    assert len(jtis) == 2


async def test_combined_pem_carries_both_certificate_and_key(certificate) -> None:
    cert_pem, key_pem = certificate
    tenant = FakeGraphTenant(certificate_pem=cert_pem)
    source = _source(tenant, certificate_pem=cert_pem + key_pem)
    assert await source.token() in tenant.issued_tokens


async def test_certificate_is_preferred_over_secret(certificate) -> None:
    cert_pem, key_pem = certificate
    tenant = FakeGraphTenant(certificate_pem=cert_pem, client_secret="s3cr3t-value")
    source = _source(
        tenant, certificate_pem=cert_pem, private_key_pem=key_pem, client_secret="s3cr3t-value"
    )
    await source.token()
    assert "client_assertion" in tenant.token_requests[-1]
    assert "client_secret" not in tenant.token_requests[-1]


async def test_unreadable_certificate_is_an_auth_error_without_the_pem() -> None:
    tenant = FakeGraphTenant()
    source = _source(tenant, certificate_pem="-----BEGIN CERTIFICATE-----\nnope\n")
    with pytest.raises(AuthError) as info:
        await source.token()
    assert "nope" not in str(info.value)
    assert tenant.token_requests == []


# ── client secret fallback ────────────────────────────────────────────────


async def test_client_secret_flow() -> None:
    tenant = FakeGraphTenant(client_secret="s3cr3t-value")
    source = _source(tenant, client_secret="s3cr3t-value")
    assert await source.token() in tenant.issued_tokens
    form = tenant.token_requests[-1]
    assert form["grant_type"] == "client_credentials"
    assert form["client_id"] == CLIENT_ID
    assert form["client_secret"] == "s3cr3t-value"


async def test_no_credential_at_all_is_an_auth_error() -> None:
    tenant = FakeGraphTenant()
    with pytest.raises(AuthError):
        await _source(tenant).token()
    assert tenant.token_requests == []


# ── safe errors ───────────────────────────────────────────────────────────


async def test_rejected_secret_is_reported_by_code_never_by_value() -> None:
    tenant = FakeGraphTenant(client_secret="the-right-one")
    source = _source(tenant, client_secret="leaked-wrong-secret-value")
    with pytest.raises(AuthError) as info:
        await source.token()
    message = str(info.value)
    assert "AADSTS7000215" in message
    assert "leaked-wrong-secret-value" not in message
    assert "Invalid client secret" not in message  # the description is never carried
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__


async def test_transport_failure_is_an_auth_error_without_the_request() -> None:
    import httpx

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    source = ClientCredentialTokenSource(
        TENANT_ID, CLIENT_ID, client_secret="sec-ret-xyz", transport=httpx.MockTransport(boom)
    )
    with pytest.raises(AuthError) as info:
        await source.token()
    assert "sec-ret-xyz" not in str(info.value)
    assert "ConnectError" in str(info.value)


async def test_auth_error_is_a_workspace_error() -> None:
    assert issubclass(AuthError, WorkspaceError)


async def test_200_without_access_token_is_an_auth_error() -> None:
    import httpx

    source = ClientCredentialTokenSource(
        TENANT_ID,
        CLIENT_ID,
        client_secret="x",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"expires_in": 3600})),
    )
    with pytest.raises(AuthError):
        await source.token()


# ── tenant validation ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "common",
        "organizations",
        "evil.com/../x",
        "contoso.com?x=1",
        "contoso.com#frag",
        "attacker.example/oauth2",
        "11111111-2222-3333-4444-55555555555",  # one digit short
        "contoso .com",
        "@contoso.com",
        "https://contoso.com",
    ],
)
def test_bad_tenant_is_rejected(bad: str) -> None:
    with pytest.raises(AuthError):
        ClientCredentialTokenSource(bad, CLIENT_ID, client_secret="x")


@pytest.mark.parametrize("good", [TENANT_ID, "contoso.onmicrosoft.com", "Contoso.COM"])
def test_good_tenant_is_accepted(good: str) -> None:
    source = ClientCredentialTokenSource(good, CLIENT_ID, client_secret="x")
    assert source.token_endpoint.startswith("https://login.microsoftonline.com/")
    assert source.token_endpoint.endswith("/oauth2/v2.0/token")


def test_bad_client_id_is_rejected() -> None:
    with pytest.raises(AuthError):
        ClientCredentialTokenSource(TENANT_ID, "not-a-guid", client_secret="x")


# ── cache, skew, single flight ────────────────────────────────────────────


async def test_token_is_cached_until_the_refresh_skew() -> None:
    tenant = FakeGraphTenant(client_secret="s", token_lifetime=3600)
    clock = Clock()
    source = _source(tenant, client_secret="s", clock=clock)

    first = await source.token()
    clock.now += 3600 - REFRESH_SKEW_SECONDS - 1
    assert await source.token() == first
    assert len(tenant.token_requests) == 1

    clock.now += 2  # now inside the skew window
    second = await source.token()
    assert second != first
    assert len(tenant.token_requests) == 2


async def test_token_shorter_than_the_skew_is_not_cached() -> None:
    tenant = FakeGraphTenant(client_secret="s", token_lifetime=120)
    source = _source(tenant, client_secret="s", clock=Clock())
    await source.token()
    await source.token()
    assert len(tenant.token_requests) == 2


async def test_forget_drops_the_cache() -> None:
    tenant = FakeGraphTenant(client_secret="s")
    source = _source(tenant, client_secret="s", clock=Clock())
    await source.token()
    source.forget()
    await source.token()
    assert len(tenant.token_requests) == 2


async def test_concurrent_callers_share_one_refresh() -> None:
    tenant = FakeGraphTenant(client_secret="s")
    source = _source(tenant, client_secret="s", clock=Clock())
    tokens = await asyncio.gather(*(source.token() for _ in range(20)))
    assert len(set(tokens)) == 1
    assert len(tenant.token_requests) == 1


async def test_loader_is_read_on_every_refresh_so_rotations_apply() -> None:
    """A credential loader (the vault) is consulted per refresh, not captured once."""
    from robothor.workspace.microsoft.auth import ClientCredentials

    tenant = FakeGraphTenant(client_secret="old")
    current = {"secret": "old"}
    calls = []

    async def loader() -> ClientCredentials:
        calls.append(1)
        return ClientCredentials(TENANT_ID, CLIENT_ID, client_secret=current["secret"])

    clock = Clock()
    source = ClientCredentialTokenSource.from_loader(
        loader, transport=tenant.transport(), clock=clock
    )
    await source.token()
    tenant.client_secret = "new"
    current["secret"] = "new"
    clock.now += 7200
    assert await source.token() in tenant.issued_tokens
    assert len(calls) == 2
    assert tenant.token_requests[-1]["client_secret"] == "new"
