"""Proving an inbound activity came from the Bot Framework, and not from anyone else.

This is the only thing standing between a public HTTPS path and
``runner.execute``. Every check below is load-bearing:

``alg``/signature
    RS256 against the keys the Bot Framework publishes, fetched through its
    OpenID metadata document. The algorithm is pinned to a fixed list, so
    ``{"alg": "none"}`` and an HS256 token signed with the *public* key — the
    two classic JWT forgeries — never reach a key lookup.

``iss``
    ``https://api.botframework.com``. A token from another Entra tenant is a
    perfectly valid token that says nothing about this bot.

``aud``
    this instance's own application id. Without it, any Teams tenant's bot token
    would open this endpoint, which is the cross-tenant version of no check
    at all.

``exp``/``nbf``
    with a small leeway for clock skew, not for convenience.

``serviceurl``
    the claim, compared against the ``serviceUrl`` in the activity body. The
    signed half is the authority. This is the check that stops a replayed
    activity from pointing the bot's own reply — bearer token attached — at an
    endpoint of the attacker's choosing.

**A failure says nothing about which check failed.** One exception type, one
message, and the route answers 401 with no body: an error that distinguishes a
bad signature from a wrong audience tells whoever is probing which half to keep
working on.

The keys are cached for :data:`KEY_CACHE_SECONDS` and refetched once when a
token names a ``kid`` the cache does not hold — Microsoft rotates these, and a
cache that never refetched would refuse every genuine activity from the moment
of a rotation until the process restarted.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt

logger = logging.getLogger(__name__)

__all__ = [
    "ALGORITHMS",
    "KEY_HOSTS",
    "ISSUER",
    "KEY_CACHE_SECONDS",
    "OPENID_METADATA_URL",
    "TokenRejectedError",
    "reset_key_cache",
    "signing_key",
    "validate_activity_token",
]

#: Where the Bot Framework publishes what signs its tokens. Channel-agnostic:
#: the Teams-specific document exists for the emulator, and this one covers the
#: tokens a real Teams tenant sends.
OPENID_METADATA_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"

#: Who the token must say it is from.
ISSUER = "https://api.botframework.com"

#: Pinned. A list that included ``none`` or an HMAC algorithm would let a
#: forger sign with the very key this module publishes.
ALGORITHMS = ["RS256"]

#: Hosts the published keys may be fetched from.
#:
#: The ``jwks_uri`` comes out of a document fetched over the network, and it was
#: accepted on ``https://`` alone — so a metadata response naming an attacker's
#: host would have this instance fetch signing keys from it and then trust tokens
#: signed with them. Reaching that requires already having broken TLS to
#: ``login.botframework.com``, at which point the document is the attacker's
#: anyway; pinning costs one comparison and makes the discovered URL as trusted
#: as the hard-coded one above it, which is what this module's docstring claims.
#:
#: Matched on the exact host or a dot-suffix of it, never ``endswith`` on the
#: bare string: ``login.botframework.com.evil.example`` ends with the suffix and
#: is not Microsoft.
KEY_HOSTS = ("login.botframework.com", "login.microsoftonline.com")

#: A day, which is what Microsoft's guidance asks clients to hold these for.
KEY_CACHE_SECONDS = 24 * 60 * 60

#: How often an UNKNOWN ``kid`` may force a refetch. Two failures pull in
#: opposite directions here and both are real. Never refetching means that the
#: moment Microsoft rotates a key, every genuine activity is refused until the
#: process restarts — with nothing in the logs naming the cause. Refetching on
#: every unknown ``kid`` means an unauthenticated caller, who chooses that
#: header, can make this instance hammer Microsoft until it is rate limited into
#: refusing every genuine activity. So: a rotation is picked up within a minute,
#: and a stream of invented key ids costs one fetch a minute.
UNKNOWN_KID_REFETCH_SECONDS = 60.0

#: Clock skew tolerated on ``exp``/``nbf``. Seconds, not minutes: this is for a
#: box whose NTP is slightly out, not for a token that has expired.
LEEWAY_SECONDS = 60

#: How long the metadata and key fetches may take.
#:
#: Five seconds, not ten, and the arithmetic is the reason: a COLD key cache
#: costs two of these in series, inside a request Teams abandons at about 15
#: seconds. At ten each, a cold start could exceed the window before the
#: activity was even acknowledged — and Teams would redeliver it, into the same
#: cold start. Ten seconds is also far longer than either fetch has any business
#: taking; a Microsoft endpoint that has not answered in five is not about to.
TIMEOUT_SECONDS = 5.0

_clock = time.monotonic

_keys: dict[str, Any] = {}
_keys_fetched_at: float = 0.0
_unknown_kid_fetched_at: float = 0.0
_lock = asyncio.Lock()


class TokenRejectedError(Exception):
    """This request is not from the Bot Framework.

    One type for every reason, carrying a message that names none of them. The
    caller turns it into a bare 401.
    """


def build_client() -> httpx.AsyncClient:
    """The HTTP client for the metadata and key fetches. Replaced in tests."""
    return httpx.AsyncClient(timeout=TIMEOUT_SECONDS)


def reset_key_cache() -> None:
    """Forget the published keys. For a rotation, and for tests."""
    global _keys_fetched_at, _unknown_kid_fetched_at
    _keys.clear()
    _keys_fetched_at = 0.0
    _unknown_kid_fetched_at = 0.0


def _is_microsoft_key_host(uri: str) -> bool:
    """Whether ``uri`` is an HTTPS URL on one of Microsoft's own key hosts.

    Parsed rather than string-matched. ``urlparse().hostname`` drops any
    ``user@`` prefix, which is how ``https://login.botframework.com@evil.example``
    reads as Microsoft to a check written with ``in`` or ``startswith``.
    """
    parsed = urlparse(uri or "")
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return any(host == known or host.endswith("." + known) for known in KEY_HOSTS)


async def _fetch_keys() -> dict[str, Any]:
    """The published keys, by ``kid``.

    Two round trips, because the metadata document is what names the key
    endpoint — hard-coding today's ``jwks_uri`` would break silently the day
    Microsoft moves it, and "silently" here means every activity refused.
    """
    async with build_client() as client:
        metadata = await client.get(OPENID_METADATA_URL)
        metadata.raise_for_status()
        jwks_uri = str((metadata.json() or {}).get("jwks_uri") or "")
        if not _is_microsoft_key_host(jwks_uri):
            logger.error(
                "Teams: the Bot Framework metadata named a key endpoint outside "
                "Microsoft's own hosts; refusing to fetch signing keys from it"
            )
            raise TokenRejectedError("the token could not be verified")
        document = await client.get(jwks_uri)
        document.raise_for_status()
        payload = document.json() or {}

    found: dict[str, Any] = {}
    for entry in payload.get("keys") or []:
        kid = str(entry.get("kid") or "")
        if not kid:
            continue
        try:
            found[kid] = jwt.PyJWK(entry).key
        except Exception as exc:  # noqa: BLE001 — one unusable key is not a failure
            logger.warning("Teams: skipping an unusable published key: %s", type(exc).__name__)
    return found


async def signing_key(kid: str) -> Any:
    """The published key ``kid`` names.

    Refetches at most once for an unknown ``kid`` (a rotation), and once the
    cache is older than :data:`KEY_CACHE_SECONDS`.

    Raises:
        TokenRejectedError: no such key, or the keys could not be fetched at all.
    """
    global _keys_fetched_at, _unknown_kid_fetched_at
    stale = (_clock() - _keys_fetched_at) > KEY_CACHE_SECONDS
    if kid in _keys and not stale:
        return _keys[kid]
    async with _lock:
        stale = (_clock() - _keys_fetched_at) > KEY_CACHE_SECONDS
        if kid in _keys and not stale:
            return _keys[kid]
        cold = not _keys or stale
        # An unknown kid on a warm cache is either a rotation or somebody
        # inventing key ids. Both get at most one fetch per
        # UNKNOWN_KID_REFETCH_SECONDS; see that constant for the trade.
        if not cold and (_clock() - _unknown_kid_fetched_at) <= UNKNOWN_KID_REFETCH_SECONDS:
            raise TokenRejectedError("the token could not be verified")
        if not cold:
            _unknown_kid_fetched_at = _clock()
        try:
            fetched = await _fetch_keys()
        except TokenRejectedError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("Teams: could not fetch the published keys: %s", type(exc).__name__)
            raise TokenRejectedError("the token could not be verified") from None
        _keys.clear()
        _keys.update(fetched)
        _keys_fetched_at = _clock()
    if kid not in _keys:
        raise TokenRejectedError("the token could not be verified")
    return _keys[kid]


def _same_service_url(claimed: str, activity_url: str) -> bool:
    """Whether the signed service URL and the activity's are the same endpoint.

    Compared with the trailing slash normalised away and case-insensitively on
    the host: Microsoft's token carries ``https://smba.trafficmanager.net/emea``
    while the activity says ``…/emea/``, and a comparison that failed on that
    would refuse every genuine activity — a self-inflicted outage wearing a
    security check's clothes. Everything else must match exactly.
    """
    return claimed.rstrip("/").lower() == activity_url.rstrip("/").lower()


async def validate_activity_token(
    authorization: str | None, activity: dict[str, Any], *, app_id: str | None
) -> dict[str, Any]:
    """Verify the bearer token on an inbound activity.

    Args:
        authorization: the raw ``Authorization`` header.
        activity: the parsed body. Only ``serviceUrl`` is read, and only to be
            checked against the claim.
        app_id: this instance's application id — the audience. An instance
            without one cannot authenticate anything and every request is
            refused, which is the correct posture for an endpoint whose whole
            job is to tell genuine from forged.

    Returns:
        The verified claims.

    Raises:
        TokenRejectedError: always with the same message. The caller answers 401
            with no body.
    """
    rejected = TokenRejectedError("the token could not be verified")
    if not app_id:
        logger.error(
            "Teams: an activity arrived but ROBOTHOR_TEAMS_APP_ID is set nowhere, "
            "so nothing can be authenticated; refusing"
        )
        raise rejected

    header = (authorization or "").strip()
    if not header.lower().startswith("bearer "):
        raise rejected
    token = header[7:].strip()
    if not token:
        raise rejected

    try:
        unverified = jwt.get_unverified_header(token)
    except Exception:  # noqa: BLE001
        raise rejected from None
    # Checked here as well as in `decode`: a `kid` lookup driven by an
    # unverified header is the one thing that happens BEFORE the signature is
    # checked, so the algorithm it claims must be one we would accept.
    if str(unverified.get("alg") or "") not in ALGORITHMS:
        raise rejected
    kid = str(unverified.get("kid") or "")
    if not kid:
        raise rejected

    key = await signing_key(kid)

    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=ALGORITHMS,
            audience=app_id,
            issuer=ISSUER,
            leeway=LEEWAY_SECONDS,
            options={"require": ["aud", "exp", "iss"]},
        )
    except Exception:  # noqa: BLE001 — every failure is the same 401
        raise rejected from None

    # The claim name is lower-case in the Bot Framework's own tokens; the
    # camel-case spelling is accepted because the emulator has used it.
    claimed = str(claims.get("serviceurl") or claims.get("serviceUrl") or "")
    activity_url = str(activity.get("serviceUrl") or "")
    if not claimed or not activity_url or not _same_service_url(claimed, activity_url):
        # The replay this whole module exists to stop: a genuine, unexpired
        # activity re-sent with the service URL swapped, so that the bot's own
        # reply — bearer token attached — goes wherever the body says.
        logger.warning("Teams: refused an activity whose serviceUrl is not the signed one")
        raise rejected

    return dict(claims)
