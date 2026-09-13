"""Telling a running engine to drop its identity caches, from the same box.

The bridge reaches the engine through ``crm/bridge/routers/_engine_client.py``,
which is a general proxy: it takes a caller-supplied path, so it carries a path
allowlist, a scheme allowlist and traversal checks to earn that generality. None
of that is importable from ``robothor/`` — the bridge runs from its own rootdir —
and none of it is needed here.

So this is the narrow version, and narrow is the security property rather than a
shortcut: it posts to ONE literal path, hardcoded below, with no body. There is
no caller-supplied path to check because there is no caller-supplied path.

Why it exists at all: ``genus channel access approve|deny|revoke`` writes a row
from the operator's shell, and the engine's belief about who a channel sender is
lives in the engine's process — 60 s in ``identity.resolvers``, **300 s** in
``engine.users``. Clearing a cache in the CLI's own process clears a cache
nobody is reading, so a revoke looked like it worked and the revoked sender kept
running for up to five minutes.

Every caller treats failure as nothing at all. The row is already durable and the
caches expire by themselves; refusing an operator's revoke because the engine is
mid-restart would be a worse outcome than the staleness this shortens.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["IDENTITY_RELOAD_PATH", "post_identity_reload"]

#: The only path this module may call. A constant and not a parameter.
IDENTITY_RELOAD_PATH = "/api/admin/identities/reload"

#: Long enough for one loopback round trip, short enough that a token scraped
#: from a process listing is worthless by the time it is read. Same reasoning as
#: the bridge's ``TOKEN_TTL_SECONDS``, one order of magnitude tighter because
#: this call has nothing to do but return.
_TOKEN_TTL_SECONDS = 30

#: The identity the engine's audit trail sees for this call.
_SERVICE_ID = "genus-cli"

_TIMEOUT_SECONDS = 5.0

#: Only these. ``ROBOTHOR_ENGINE_URL`` comes from a unit file or an operator's
#: environment, and a ``file://`` value there would make httpx resolve something
#: nobody intended. The bridge's client draws the same line for the same reason.
_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _base_url() -> str:
    """Where the engine answers. Loopback unless deployment says otherwise."""
    from urllib.parse import urlsplit

    from robothor.settings import get_settings

    engine = get_settings().engine
    explicit = (engine.url or "").strip()
    if not explicit:
        return f"http://{engine.host}:{engine.port}"
    parsed = urlsplit(explicit)
    if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.netloc:
        raise ValueError(
            f"ROBOTHOR_ENGINE_URL must be an http(s) URL with a host, got {explicit!r}"
        )
    return explicit.rstrip("/")


async def post_identity_reload() -> int:
    """Ask the engine to forget who each channel sender is. Returns the status.

    Raises only for a configuration error the operator can fix (a bad engine URL,
    no signing key). A connection failure is a status of 0 and a warning, because
    "the engine is not running" is a normal state for a box being set up and not
    a reason to refuse a revoke.
    """
    import httpx

    from robothor.auth.tokens import issue_service_token
    from robothor.constants import DEFAULT_TENANT
    from robothor.engine.auth import ENGINE_AUDIENCE

    token = issue_service_token(
        _SERVICE_ID,
        DEFAULT_TENANT,
        audience=ENGINE_AUDIENCE,
        scopes=("engine:control",),
        ttl_seconds=_TOKEN_TTL_SECONDS,
    )
    url = f"{_base_url()}{IDENTITY_RELOAD_PATH}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(url, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        logger.warning("Engine did not answer an identity cache reload: %s", type(exc).__name__)
        return 0
    if response.status_code >= 400:
        logger.warning("Engine refused an identity cache reload (status %s)", response.status_code)
    return int(response.status_code)
