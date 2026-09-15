"""The Entra token this channel sends with, and the only place it is held.

One client-credentials grant against ``login.microsoftonline.com``, exchanged
for a bearer token scoped to ``https://api.botframework.com/.default``. Three
properties are worth stating, because each one is a failure this platform has
seen on some other surface:

**Cached until shortly before it expires.** Not per send: a briefing chunked
into four activities would otherwise be four token requests, and Entra rate
limits. Not until it expires either — :data:`REFRESH_SKEW_SECONDS` earlier, or a
token refreshed at the instant of expiry has already expired by the time the
activity lands.

**On a monotonic clock.** A wall clock that steps backwards (NTP, a suspended
laptop, a container migrated) makes a live token look expired forever, or worse,
an expired one look live.

**Never logged, never returned, never in an error.** Entra's own
``error_description`` quotes the secret it rejected back at you, verbatim — and
the platform's pattern-based ``redact`` cannot catch it, because a client secret
has no shape to match. So :func:`_describe` scrubs the value this instance holds
by identity first and runs the pattern pass after it, and what survives is the
``error`` code, which is the word that tells an operator what to fix.
:meth:`TokenSource.last_attempt` exists so ``health()`` can report *whether* the
last fetch worked without anything nearby holding the token.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from genus_teams.credentials import teams_credentials

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

logger = logging.getLogger(__name__)

__all__ = [
    "REFRESH_SKEW_SECONDS",
    "SCOPE",
    "TOKEN_URL",
    "TokenError",
    "TokenSource",
    "build_client",
]

#: The client-credentials endpoint, per directory.
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

#: What a bot asks for. The Bot Framework accepts nothing else.
SCOPE = "https://api.botframework.com/.default"

#: How long before expiry a cached token is considered spent. Big enough to
#: cover a slow activity POST on a congested link; small enough that a
#: five-minute token (which Entra does issue under load) is still cached.
REFRESH_SKEW_SECONDS = 300.0

#: Every HTTP call this plugin makes is bounded. A channel that blocks forever
#: on a hung socket holds the delivery path, and on the inbound side it holds a
#: request Teams will abandon after 15 seconds anyway.
TIMEOUT_SECONDS = 10.0


class TokenError(RuntimeError):
    """A token could not be obtained. Carries a code, never a credential."""


def build_client() -> httpx.AsyncClient:
    """The HTTP client. The seam the suite replaces with a fixture transport.

    A function rather than a module-level client so that nothing opens a socket
    at import time — ``robothor/engine/channels/base.py`` is explicit that a
    channel must open its transport lazily inside ``send``.
    """
    return httpx.AsyncClient(timeout=TIMEOUT_SECONDS)


#: What a scrubbed credential is replaced with. Visible on purpose: an operator
#: who can see that something was removed knows the message was edited, where a
#: silently truncated sentence just looks like a bad error message.
REDACTED = "<client secret>"


def _describe(payload: Any, status: int, secret: str | None = None) -> str:
    """What went wrong, in words safe to print.

    Two passes, and the first is the one that matters. Entra quotes the secret
    it rejected back at you, in full, inside ``error_description``:

        AADSTS7000215: Invalid client secret provided. [<the secret>]

    ``robothor.secrets.redaction.redact`` cannot help with that — it matches the
    SHAPES of known credentials (``xoxb-``, ``sk-``, a bot token), and an Entra
    client secret is an arbitrary string with no shape at all. So the value this
    instance holds is scrubbed by identity first, and the pattern pass runs
    after to catch anything else the response carried.

    The ``error`` code (``invalid_client``, ``unauthorized_client``) is kept
    because it is the half that names the fix.
    """
    from robothor.secrets.redaction import redact

    def _safe(text: str) -> str:
        cleaned = text.replace(secret, REDACTED) if secret and secret in text else text
        return redact(cleaned)

    if isinstance(payload, dict):
        code = str(payload.get("error") or "")
        description = str(payload.get("error_description") or "")
        if code:
            return _safe(f"{code}: {description}" if description else code)
        if description:
            return _safe(description)
    return f"HTTP {status}"


class TokenSource:
    """A cached bot token for one instance.

    Not a module-level singleton: the channel owns one, and a second instance in
    a test or a second tenant in a future multi-tenant deployment gets its own
    cache rather than silently sharing one.
    """

    def __init__(
        self,
        *,
        tenant_id: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tenant_id = tenant_id
        self._clock = clock
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()
        self._last_ok: bool | None = None
        self._last_error: str = ""
        self._last_at: float = 0.0

    def last_attempt(self) -> dict[str, Any]:
        """Whether the last fetch worked, for ``health()``. No token, ever."""
        return {
            "ok": self._last_ok,
            "error": self._last_error,
            "seconds_ago": round(self._clock() - self._last_at, 1) if self._last_at else None,
            "cached": bool(self._token) and self._clock() < self._expires_at,
        }

    def forget(self) -> None:
        """Drop the cached token — for a rotation, and for tests."""
        self._token = None
        self._expires_at = 0.0

    async def token(self) -> str:
        """A live bot token.

        Raises:
            TokenError: the credentials are missing, Entra refused them, or the
                request failed. Never carries a credential.
        """
        if self._token and self._clock() < self._expires_at:
            return self._token
        async with self._lock:
            # Re-checked inside the lock: a burst of chunked sends would
            # otherwise make one token request each while the first was in
            # flight, which is how a channel gets itself rate limited.
            if self._token and self._clock() < self._expires_at:
                return self._token
            return await self._fetch()

    async def _fetch(self) -> str:
        kwargs = {"tenant_id": self._tenant_id} if self._tenant_id else {}
        credentials = teams_credentials(**kwargs)  # type: ignore[arg-type]
        if not credentials.can_send:
            self._record(False, "the application id or the client secret is set nowhere")
            raise TokenError(
                "Teams is not configured on this instance: "
                "ROBOTHOR_TEAMS_APP_ID / ROBOTHOR_TEAMS_APP_PASSWORD are set "
                "neither in the environment nor in this instance's vault"
            )

        form = {
            "grant_type": "client_credentials",
            "client_id": credentials.app_id or "",
            "client_secret": credentials.app_password or "",
            "scope": SCOPE,
        }
        url = TOKEN_URL.format(tenant=credentials.token_tenant)
        try:
            async with build_client() as client:
                response = await client.post(url, data=form)
        except Exception as exc:  # noqa: BLE001 — a transport failure is a report
            detail = f"{type(exc).__name__}"
            self._record(False, detail)
            raise TokenError(f"the token request failed: {detail}") from None

        if response.status_code != 200:
            detail = _describe(_json(response), response.status_code, credentials.app_password)
            self._record(False, detail)
            # `from None`: the httpx exception chain would carry the request,
            # and the request body is the client secret.
            raise TokenError(f"Entra refused the credentials: {detail}") from None

        payload = _json(response)
        token = str(payload.get("access_token") or "") if isinstance(payload, dict) else ""
        if not token:
            self._record(False, "no access_token in the response")
            raise TokenError("Entra answered 200 with no access_token")

        expires_in = 0.0
        if isinstance(payload, dict):
            try:
                expires_in = float(payload.get("expires_in") or 0)
            except (TypeError, ValueError):
                expires_in = 0.0
        # A response with no usable lifetime is cached for nothing rather than
        # forever: an assumed hour on a five-minute token is 55 minutes of 401s.
        self._token = token
        self._expires_at = self._clock() + max(0.0, expires_in - REFRESH_SKEW_SECONDS)
        self._record(True, "")
        return token

    def _record(self, ok: bool, error: str) -> None:
        self._last_ok = ok
        self._last_error = error
        self._last_at = self._clock()
        if ok:
            logger.info("Teams: bot token refreshed")
        else:
            # `error` has already been through redaction.
            logger.error("Teams: could not obtain a bot token: %s", error)


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:  # noqa: BLE001 — a non-JSON body is a status code and nothing more
        return None
