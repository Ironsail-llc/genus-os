"""Channel-native identifier → unified ``IdentityContext``.

Each channel speaks its own identifier: a ``user_accounts.id`` UUID for
webchat, a Telegram numeric user id, a vision face label. ``resolve_identity``
is the one entry point every channel calls to turn that native identifier
into the shared ``IdentityContext`` shape, delegating to the platform's
existing identity DALs (``robothor.auth.accounts``, ``robothor.engine.users``)
rather than re-implementing lookups.

Never raises: an unknown channel, a missing row, or a DB error all resolve to
``None`` — this sits on the hot path of every channel's message handling and
must not be able to take a run down.

Results are cached in-process, keyed by ``(channel, identifier, tenant_id)``,
including negative (``None``) results, so a burst of messages from the same
sender doesn't hit the database every time.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import psycopg2
from psycopg2.extras import RealDictCursor

from robothor.auth.accounts import get_account_by_id
from robothor.db.connection import get_connection
from robothor.engine.users import lookup_user
from robothor.identity.context import IdentityContext

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60

_cache: dict[tuple[str, str, str], tuple[IdentityContext | None, float]] = {}


def resolve_identity(channel: str, identifier: str, tenant_id: str) -> IdentityContext | None:
    """Resolve a channel-native identifier to an ``IdentityContext``.

    Returns ``None`` for an unrecognized channel, an identifier with no
    matching account/user row, or if resolution fails for any reason —
    never raises.
    """
    cache_key = (channel, identifier, tenant_id)
    cached = _cache.get(cache_key)
    if cached is not None:
        value, expires_at = cached
        if time.monotonic() < expires_at:
            return value
        del _cache[cache_key]

    resolver = _RESOLVERS.get(channel)
    if resolver is None:
        result = None
    else:
        try:
            result = resolver(identifier, tenant_id)
        except Exception:
            logger.exception("resolve_identity: resolver for channel %r failed", channel)
            result = None

    _cache[cache_key] = (result, time.monotonic() + _CACHE_TTL_SECONDS)
    return result


def clear_cache() -> None:
    """Clear the identity resolution cache (e.g. after account changes)."""
    _cache.clear()


def _resolve_webchat(identifier: str, tenant_id: str) -> IdentityContext | None:
    """webchat identifier = ``user_accounts.id``. DB-verified: always True."""
    account = get_account_by_id(identifier)
    if not account:
        return None
    if account.get("tenant_id") != tenant_id:
        return None
    if account.get("status") != "active":
        return None

    person_id = account.get("person_id")
    return IdentityContext(
        tenant_id=tenant_id,
        channel="webchat",
        identifier=identifier,
        verified=True,
        display_name=account.get("display_name") or "",
        role=account.get("role") or "",
        user_account_id=str(account["id"]),
        person_id=str(person_id) if person_id else None,
        email=account.get("email"),
    )


def _resolve_telegram(identifier: str, tenant_id: str) -> IdentityContext | None:
    """telegram identifier = the sender's Telegram user id."""
    info = lookup_user(identifier, tenant_id=tenant_id)
    if info is None:
        return None

    person_id = info.get("person_id")
    email: str | None = None
    user_account_id: str | None = None
    if person_id:
        # Opportunistic single-query join — best-effort, never fails the
        # overall resolution if user_accounts has no matching row.
        try:
            with get_connection() as conn:
                cur = conn.cursor(cursor_factory=RealDictCursor)
                cur.execute(
                    "SELECT id, email FROM user_accounts "
                    "WHERE person_id = %s AND tenant_id = %s LIMIT 1",
                    (person_id, tenant_id),
                )
                row = cur.fetchone()
            if row:
                user_account_id = str(row["id"])
                email = row["email"]
        except Exception:
            logger.exception(
                "_resolve_telegram: opportunistic user_accounts join failed for person_id %s",
                person_id,
            )

    return IdentityContext(
        tenant_id=tenant_id,
        channel="telegram",
        identifier=identifier,
        verified=True,
        display_name=info.get("display_name") or "",
        role=info.get("role") or "",
        tenant_user_id=info.get("user_id"),
        person_id=str(person_id) if person_id else None,
        user_account_id=user_account_id,
        email=email,
    )


def _resolve_vision(identifier: str, tenant_id: str) -> IdentityContext | None:
    """vision identifier = a face label.

    ``face_identities`` does not exist yet (a later phase adds the
    migration) — probe for it with ``to_regclass`` and return ``None``
    gracefully rather than raising, so this module ships ahead of that
    migration. Always ``verified=False``: a face match is probabilistic,
    never DB/crypto-verified.
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT to_regclass('public.face_identities')")
            row = cur.fetchone()
            if not row or row[0] is None:
                return None
            cur.execute(
                "SELECT person_id, display_name FROM face_identities "
                "WHERE tenant_id = %s AND face_label = %s LIMIT 1",
                (tenant_id, identifier),
            )
            match = cur.fetchone()
    except (psycopg2.errors.UndefinedTable, psycopg2.Error):
        logger.debug("_resolve_vision: face_identities unavailable", exc_info=True)
        return None

    if not match:
        return None
    person_id, display_name = match
    return IdentityContext(
        tenant_id=tenant_id,
        channel="vision",
        identifier=identifier,
        verified=False,
        display_name=display_name or "",
        person_id=str(person_id) if person_id else None,
    )


_RESOLVERS: dict[str, Callable[[str, str], IdentityContext | None]] = {
    "webchat": _resolve_webchat,
    "telegram": _resolve_telegram,
    "vision": _resolve_vision,
}
