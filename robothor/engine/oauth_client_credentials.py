"""OAuth 2.0 client_credentials tokens for http MCP adapters.

Some MCP servers do not take a long-lived static bearer token: they hand an
operator a ``client_id`` / ``client_secret`` pair and expect the client to mint
short-lived access tokens at a token endpoint (RFC 6749 §4.4). This module is
that client: a generic, provider-neutral version of
``robothor.workspace.microsoft.auth.ClientCredentialTokenSource`` that takes any
``token_url`` and ``scope`` and authenticates with the secret in the form body
(``client_secret_post``).

The cache follows the Microsoft source:

* cached until :data:`REFRESH_SKEW_SECONDS` before ``expires_in``, on a
  **monotonic** clock, so a stepped wall clock cannot keep a dead token alive;
* one mint at a time under an :class:`asyncio.Lock`, re-checked inside the
  lock, so a burst of calls makes one token request;
* errors carry the OAuth ``error`` code and the HTTP status and nothing else --
  servers echo rejected credentials in ``error_description``, so it is never
  carried. Neither the secret nor a token is ever logged.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

__all__ = [
    "AUTH_TYPE",
    "REFRESH_SKEW_SECONDS",
    "OAuthClientCredentials",
    "OAuthClientCredentialsTokenSource",
    "OAuthTokenError",
]

#: The ``auth.type`` value an adapter YAML uses to select this grant.
AUTH_TYPE = "oauth_client_credentials"

#: How long before expiry a cached token is considered spent.
REFRESH_SKEW_SECONDS = 60.0

#: Every token request is bounded.
TIMEOUT_SECONDS = 10.0

_OAUTH_ERROR = re.compile(r"^[a-z_]{1,64}$")


class OAuthTokenError(RuntimeError):
    """A token could not be minted. The message never carries a credential."""


@dataclass(frozen=True)
class OAuthClientCredentials:
    """One client registration. ``repr`` never shows the secret."""

    token_url: str
    client_id: str
    client_secret: str = field(repr=False)
    scope: str = ""


class OAuthClientCredentialsTokenSource:
    """A cached access token for one client at one token endpoint."""

    def __init__(
        self,
        credentials: OAuthClientCredentials,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._credentials = credentials
        self._transport = transport
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    def forget(self) -> None:
        """Drop the cached token -- after a 401, a rotation, or in a test."""
        self._token = None
        self._expires_at = 0.0

    async def token(self) -> str:
        """A live access token. Raises :class:`OAuthTokenError`, never with a credential."""
        if self._token and self._clock() < self._expires_at:
            return self._token
        async with self._lock:
            if self._token and self._clock() < self._expires_at:
                return self._token
            return await self._fetch()

    async def _fetch(self) -> str:
        creds = self._credentials
        form = {
            "grant_type": "client_credentials",
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
        }
        if creds.scope:
            form["scope"] = creds.scope

        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=TIMEOUT_SECONDS, follow_redirects=False
            ) as client:
                response = await client.post(
                    creds.token_url, data=form, headers={"Accept": "application/json"}
                )
        except Exception as exc:  # noqa: BLE001 - a transport failure is a report
            # `from None`: the exception chain would carry the request, and the
            # request body is the secret.
            raise OAuthTokenError(f"the token request failed: {type(exc).__name__}") from None

        payload = _json(response)
        if response.status_code != 200:
            detail = _describe(payload, response.status_code)
            logger.error(
                "oauth client_credentials: token endpoint refused client %s: %s",
                creds.client_id,
                detail,
            )
            raise OAuthTokenError(f"the token endpoint refused the credentials: {detail}")

        token = str(payload.get("access_token") or "") if isinstance(payload, dict) else ""
        if not token:
            raise OAuthTokenError("the token endpoint answered 200 with no access_token")
        try:
            expires_in = float(payload.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0.0
        self._token = token
        # A token with no usable lifetime is cached for nothing, never forever.
        self._expires_at = self._clock() + max(0.0, expires_in - REFRESH_SKEW_SECONDS)
        logger.info("oauth client_credentials: token minted for client %s", creds.client_id)
        return token


def _describe(payload: Any, status: int) -> str:
    """The OAuth ``error`` code and the status; never ``error_description``."""
    if isinstance(payload, dict):
        code = str(payload.get("error") or "")
        if _OAUTH_ERROR.fullmatch(code):
            return f"{code} (HTTP {status})"
    return f"HTTP {status}"


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:  # noqa: BLE001 - a non-JSON body is a status code and nothing more
        return None
