"""Dropping the engine's identity caches from outside the engine.

A revoked channel binding is a row, and the bridge or the CLI can write it from
their own process perfectly well. What they cannot do is make the ENGINE stop
believing the old answer: ``robothor.identity.resolvers`` caches a resolved
identity for 60 s and ``robothor.engine.users`` caches a ``tenant_users`` row for
**300 s**, both in the process that read them. So an operator who revoked a
binding from the Helm watched the revoke succeed and the revoked sender went on
driving the agent for up to five minutes, with nothing anywhere saying why.

That is the same class of defect as ``admin_approvals``: the decision was
durable, and the process that had to act on it was never told. So it crosses the
boundary the same way — a call the writer makes, through
``_engine_client.engine_request``, under ``/api/admin``, which
``engine/auth.py`` gates on ``engine:control``.

**Best-effort by design.** The caller must not fail an approval because the
engine is restarting: the row is already written and the caches expire on their
own within 300 s. So this route exists to shorten a window, not to be the thing
that closes it, and every caller ignores its failure and says what it did.

Its own module rather than more closures in ``health.py``, for the reason
``admin_scheduler`` gives: that file is the engine's largest, and the surface
that decides who the engine thinks is talking to it should not be hard to find
inside it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

__all__ = ["register", "reload_identity_caches"]


def reload_identity_caches() -> dict[str, Any]:
    """Drop every in-process cache that could hold a stale identity.

    Both of them, named rather than "the identity cache": they have different
    TTLs (60 s and 300 s) and different keys, and clearing one while believing
    it was both is how a Telegram revoke would appear to work on Slack and not
    on Telegram.
    """
    from robothor.engine.users import clear_cache as clear_tenant_users_cache
    from robothor.identity.resolvers import clear_cache as clear_resolver_cache

    clear_resolver_cache()
    clear_tenant_users_cache()
    logger.info("identity caches dropped on request")
    return {"reloaded": True, "caches": ["identity_resolvers", "tenant_users"]}


def register(app: FastAPI) -> None:
    """Mount the identity-cache reload route on the engine app."""
    import asyncio

    from fastapi import APIRouter

    router = APIRouter(prefix="/api/admin", tags=["admin"])

    @router.post("/identities/reload")
    async def reload_identities() -> dict[str, Any]:
        """Forget who the engine thinks each channel sender is.

        Called after a pairing is approved, denied or revoked somewhere else, so
        the next inbound message resolves against the rows as they are now
        rather than as they were up to five minutes ago.
        """
        return await asyncio.to_thread(reload_identity_caches)

    app.include_router(router)
