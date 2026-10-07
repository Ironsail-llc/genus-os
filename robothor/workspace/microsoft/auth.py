"""Entra ID app-only tokens for Microsoft Graph (OAuth 2.0 client credentials).

The workspace transport runs as an *application*, not as a signed-in user: the
client's Entra admin consents once, and Exchange RBAC for Applications scopes
the grant to exactly the assistant's and the owner's mailboxes (see
``docs/workspace/microsoft365.md``). This module turns the app's credential
into a bearer token and holds it.

**Certificate first.** The preferred credential is an X.509 certificate whose
private key signs a short-lived client assertion (RFC 7523): a JWT with
``aud`` = the token endpoint, ``iss`` = ``sub`` = the client id, a fresh
``jti`` and a ten-minute ``exp``, its header naming the certificate by
thumbprint (``x5t#S256``, and the SHA-1 ``x5t`` Entra has always matched
``RS256`` assertions on). The secret never leaves this process; only a
signature does. A ``client_secret`` is accepted as a fallback because some
tenants start there.

The cache follows ``plugins/genus-teams/genus_teams/tokens.py`` (the patterns
are copied, the plugin is not imported -- core must not depend on a plugin):

* cached until :data:`REFRESH_SKEW_SECONDS` before expiry, on a **monotonic**
  clock, so a stepped wall clock cannot keep a dead token alive;
* one refresh at a time under an :class:`asyncio.Lock`, re-checked inside the
  lock, so a burst of calls makes one token request;
* errors carry Entra's ``error`` code and the ``AADSTSnnnnn`` identifier and
  nothing else -- Entra's ``error_description`` quotes a rejected secret back
  verbatim, so it is never carried, not even scrubbed.

Credentials can come from a *loader* (the vault, see
:func:`robothor.workspace.microsoft.graph_client_from_vault`) that is consulted
on every refresh, so a rotated certificate or secret applies at the next
refresh without a restart -- the pattern ``robothor/sales/providers.py`` uses.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from robothor.workspace.errors import AuthError

logger = logging.getLogger(__name__)

__all__ = [
    "ASSERTION_LIFETIME_SECONDS",
    "ASSERTION_TYPE",
    "GRAPH_SCOPE",
    "REFRESH_SKEW_SECONDS",
    "ClientCredentialTokenSource",
    "ClientCredentials",
    "TokenSource",
    "token_url",
    "validate_tenant_id",
]

#: The client-credentials endpoint, per directory.
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

#: Everything the app registration was granted on Graph.
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

#: RFC 7523 client-assertion type.
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"

#: A client assertion lives this long. Entra rejects long-lived assertions, and
#: one is minted per token request anyway.
ASSERTION_LIFETIME_SECONDS = 600

#: How long before expiry a cached token is considered spent.
REFRESH_SKEW_SECONDS = 300.0

#: Every token request is bounded.
TIMEOUT_SECONDS = 10.0

_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
#: A verified domain: dot-separated DNS labels ending in an alphabetic TLD.
#: Deliberately excludes ``common``/``organizations``/``consumers`` (no dot),
#: which are multi-tenant aliases an app-only grant must never use.
_DOMAIN = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_AADSTS_CODE = re.compile(r"AADSTS\d{4,7}")
_OAUTH_ERROR = re.compile(r"^[a-z_]{1,64}$")
_PEM_CERT = re.compile(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", re.DOTALL)
_PEM_KEY = re.compile(
    r"-----BEGIN (?:RSA |ENCRYPTED )?PRIVATE KEY-----.+?-----END (?:RSA |ENCRYPTED )?PRIVATE KEY-----",
    re.DOTALL,
)


class TokenSource(Protocol):
    """Anything that can hand the Graph client a live bearer token."""

    async def token(self) -> str: ...


def validate_tenant_id(tenant_id: str) -> str:
    """The directory id, normalised, or :class:`AuthError`.

    A GUID or a verified domain, nothing else: the value is interpolated into
    the token URL's path, so a ``/``, ``?``, ``#`` or ``@`` here would point
    the client assertion (or the secret) at a different endpoint.
    """
    candidate = str(tenant_id or "").strip().lower()
    if _GUID.fullmatch(candidate) or _DOMAIN.fullmatch(candidate):
        return candidate
    raise AuthError("microsoft365 tenant id must be a directory GUID or a verified domain")


def _validate_client_id(client_id: str) -> str:
    candidate = str(client_id or "").strip().lower()
    if not _GUID.fullmatch(candidate):
        raise AuthError("microsoft365 client id must be the application (client) GUID")
    return candidate


def token_url(tenant_id: str) -> str:
    """The token endpoint for one directory (validated)."""
    return TOKEN_URL.format(tenant=validate_tenant_id(tenant_id))


@dataclass(frozen=True)
class ClientCredentials:
    """One app registration's credential. ``repr`` never shows a secret."""

    tenant_id: str
    client_id: str
    certificate_pem: str | None = None
    private_key_pem: str | None = None
    client_secret: str | None = None

    def __repr__(self) -> str:
        kind = "certificate" if self.certificate_pem else "secret" if self.client_secret else "none"
        return f"ClientCredentials(tenant_id={self.tenant_id!r}, client_id={self.client_id!r}, kind={kind})"

    def validated(self) -> ClientCredentials:
        return ClientCredentials(
            validate_tenant_id(self.tenant_id),
            _validate_client_id(self.client_id),
            self.certificate_pem or None,
            self.private_key_pem or None,
            self.client_secret or None,
        )


CredentialLoader = Callable[[], Awaitable[ClientCredentials]]


class ClientCredentialTokenSource:
    """A cached Graph token for one app registration in one directory."""

    def __init__(
        self,
        tenant_id: str | None = None,
        client_id: str | None = None,
        *,
        certificate_pem: str | None = None,
        private_key_pem: str | None = None,
        client_secret: str | None = None,
        scope: str = GRAPH_SCOPE,
        loader: CredentialLoader | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        assertion_algorithm: Literal["RS256", "PS256"] = "RS256",
    ) -> None:
        if loader is None:
            self._static: ClientCredentials | None = ClientCredentials(
                str(tenant_id or ""),
                str(client_id or ""),
                certificate_pem,
                private_key_pem,
                client_secret,
            ).validated()
            self.token_endpoint: str | None = token_url(self._static.tenant_id)
        else:
            if tenant_id or client_id or certificate_pem or private_key_pem or client_secret:
                raise TypeError("pass either a loader or static credentials, not both")
            self._static = None
            self.token_endpoint = None
        if assertion_algorithm not in ("RS256", "PS256"):
            raise ValueError("assertion_algorithm must be RS256 or PS256")
        self._loader = loader
        self._scope = scope
        self._transport = transport
        self._clock = clock
        self._wall_clock = wall_clock
        self._algorithm = assertion_algorithm
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    @classmethod
    def from_loader(cls, loader: CredentialLoader, **kwargs: Any) -> ClientCredentialTokenSource:
        """A source whose credentials are re-read on every refresh (rotation-safe)."""
        return cls(loader=loader, **kwargs)

    def forget(self) -> None:
        """Drop the cached token -- after a 401, a rotation, or in a test."""
        self._token = None
        self._expires_at = 0.0

    async def token(self) -> str:
        """A live bearer token. Raises :class:`AuthError`, never with a credential."""
        if self._token and self._clock() < self._expires_at:
            return self._token
        async with self._lock:
            if self._token and self._clock() < self._expires_at:
                return self._token
            return await self._fetch()

    # ── internals ────────────────────────────────────────────────────────

    async def _credentials(self) -> ClientCredentials:
        if self._static is not None:
            return self._static
        assert self._loader is not None
        return (await self._loader()).validated()

    async def _fetch(self) -> str:
        creds = await self._credentials()
        url = token_url(creds.tenant_id)
        form = {
            "grant_type": "client_credentials",
            "client_id": creds.client_id,
            "scope": self._scope,
        }
        if creds.certificate_pem:
            form["client_assertion_type"] = ASSERTION_TYPE
            form["client_assertion"] = self._assertion(creds, url)
        elif creds.client_secret:
            form["client_secret"] = creds.client_secret
        else:
            raise AuthError("microsoft365 has no certificate or client secret configured")

        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=TIMEOUT_SECONDS, follow_redirects=False
            ) as client:
                response = await client.post(url, data=form)
        except Exception as exc:  # noqa: BLE001 - a transport failure is a report
            # `from None`: the exception chain would carry the request, and the
            # request body is the assertion or the secret.
            raise AuthError(f"the Entra token request failed: {type(exc).__name__}") from None

        payload = _json(response)
        if response.status_code != 200:
            detail = _describe(payload, response.status_code)
            logger.error("microsoft365: Entra refused the app credentials: %s", detail)
            raise AuthError(f"Entra refused the credentials: {detail}") from None

        token = str(payload.get("access_token") or "") if isinstance(payload, dict) else ""
        if not token:
            raise AuthError("Entra answered 200 with no access_token")
        try:
            expires_in = float(payload.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0.0
        self._token = token
        # A token with no usable lifetime is cached for nothing, never forever.
        self._expires_at = self._clock() + max(0.0, expires_in - REFRESH_SKEW_SECONDS)
        logger.info("microsoft365: Graph token refreshed")
        return token

    def _assertion(self, creds: ClientCredentials, audience: str) -> str:
        """A signed RFC 7523 client assertion for one token request."""
        import jwt
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        cert_pem = creds.certificate_pem or ""
        key_source = creds.private_key_pem or cert_pem
        cert_block = _PEM_CERT.search(cert_pem)
        key_block = _PEM_KEY.search(key_source)
        if cert_block is None or key_block is None:
            raise AuthError("the microsoft365 certificate or its private key could not be read")
        try:
            cert = x509.load_pem_x509_certificate(cert_block.group(0).encode())
            key = serialization.load_pem_private_key(key_block.group(0).encode(), password=None)
        except Exception:  # noqa: BLE001 - never echo PEM material
            raise AuthError(
                "the microsoft365 certificate or its private key could not be read"
            ) from None
        if not isinstance(key, rsa.RSAPrivateKey):
            raise AuthError("the microsoft365 private key must be RSA")
        if cert.public_key().public_numbers() != key.public_key().public_numbers():  # type: ignore[union-attr]
            raise AuthError("the microsoft365 private key does not match the certificate")

        der = cert.public_bytes(serialization.Encoding.DER)
        headers = {"x5t#S256": _b64url(hashlib.sha256(der).digest())}
        if self._algorithm == "RS256":
            headers["x5t"] = _b64url(cert.fingerprint(hashes.SHA1()))  # noqa: S303 - an identifier, not a signature
        now = int(self._wall_clock())
        claims = {
            "aud": audience,
            "iss": creds.client_id,
            "sub": creds.client_id,
            "jti": str(uuid.uuid4()),
            "iat": now,
            "nbf": now,
            "exp": now + ASSERTION_LIFETIME_SECONDS,
        }
        return jwt.encode(claims, key, algorithm=self._algorithm, headers=headers)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _describe(payload: Any, status: int) -> str:
    """Entra's error code and AADSTS identifier; never its description."""
    if isinstance(payload, dict):
        raw_code = str(payload.get("error") or "")
        code = raw_code if _OAUTH_ERROR.fullmatch(raw_code) else ""
        found = _AADSTS_CODE.search(str(payload.get("error_description") or ""))
        detail = ": ".join(part for part in (code, found.group(0) if found else "") if part)
        if detail:
            return detail
    return f"HTTP {status}"


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:  # noqa: BLE001 - a non-JSON body is a status code and nothing more
        return None
