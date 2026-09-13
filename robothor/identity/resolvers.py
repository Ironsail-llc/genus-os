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
import uuid as _uuid
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

_CACHE_TTL_SECONDS = 60.0

#: Postgres regex for a UUID in its canonical text form. Guards the
#: ``uci.user_id::UUID`` cast in ``_resolve_generic``: that column holds either a
#: ``user_accounts.id`` or a ``tenant_users.user_id``, and casting the second
#: kind would raise ``InvalidTextRepresentation`` for a perfectly valid row.
_UUID_TEXT = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

_cache: dict[tuple[str, str, str], tuple[IdentityContext | None, float]] = {}


def resolve_identity(
    channel: str,
    identifier: str,
    tenant_id: str,
    *,
    negative_ttl_seconds: float | None = None,
) -> IdentityContext | None:
    """Resolve a channel-native identifier to an ``IdentityContext``.

    ``negative_ttl_seconds`` shortens how long a MISS is remembered, for the
    caller that cares: the channel access gate. A sender waiting on a pairing
    is by definition a miss, and caching that for the full 60 s means the
    operator approves them and their next message is still refused — and, worse,
    mints a second pending code that shows up as a second request. A hit keeps
    the full TTL either way: the expensive thing to be wrong about is an
    identity that no longer exists, and that is what
    ``/api/admin/identities/reload`` is for.

    Returns ``None`` for an identifier with no matching account/user/binding
    row, or if resolution fails for any reason — never raises. A channel with
    no dedicated resolver is no longer a special case: it falls to
    :func:`_resolve_generic`, which answers out of ``user_channel_identities``
    and returns ``None`` when there is nothing there.
    """
    cache_key = (channel, identifier, tenant_id)
    cached = _cache.get(cache_key)
    if cached is not None:
        value, expires_at = cached
        if time.monotonic() < expires_at:
            return value
        del _cache[cache_key]

    try:
        result = resolver_for(channel)(identifier, tenant_id)
    except Exception:
        logger.exception("resolve_identity: resolver for channel %r failed", channel)
        result = None

    ttl = _CACHE_TTL_SECONDS
    if result is None and negative_ttl_seconds is not None:
        ttl = min(ttl, max(0.0, negative_ttl_seconds))
    _cache[cache_key] = (result, time.monotonic() + ttl)
    return result


def clear_cache() -> None:
    """Clear the identity resolution cache (e.g. after account changes)."""
    _cache.clear()


def _resolve_webchat(identifier: str, tenant_id: str) -> IdentityContext | None:
    """webchat identifier = ``user_accounts.id``. DB-verified: always True.

    ``user_accounts.id`` is a UUID column — validate the identifier's shape
    before querying so a non-UUID (e.g. a service caller's ``"service:<agent>"``
    marker slipping through) returns None immediately instead of round-tripping
    to Postgres and raising ``InvalidTextRepresentation``, which the caller
    catches but logs at ``exception`` level on every occurrence.
    """
    try:
        _uuid.UUID(str(identifier))
    except (ValueError, AttributeError, TypeError):
        logger.debug(
            "_resolve_webchat: identifier %r is not a UUID, skipping DB lookup", identifier
        )
        return None

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

    ``face_identities`` (migration 089, Task 7) maps a vision-service face
    label to a ``crm_people`` row, written by the engine's vision tool
    handlers (``enroll_face``/``unenroll_face``) and the
    ``robothor user link-face`` CLI. Still probed with ``to_regclass`` and
    returns ``None`` gracefully on a DB error or an environment that hasn't
    applied the migration yet — this sits on the hot path of every vision
    identity lookup and must never raise. Always ``verified=False``: a face
    match is probabilistic, never DB/crypto-verified. ``display_name``
    prefers the row's own value and falls back (single query, ``LEFT JOIN
    crm_people`` + ``COALESCE``) to the linked person's first+last name when
    the row was upserted with an empty display_name.
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT to_regclass('public.face_identities')")
            row = cur.fetchone()
            if not row or row[0] is None:
                return None
            cur.execute(
                "SELECT fi.person_id, "
                "COALESCE(NULLIF(fi.display_name, ''), "
                "NULLIF(TRIM(CONCAT_WS(' ', cp.first_name, cp.last_name)), ''), '') "
                "AS display_name "
                "FROM face_identities fi "
                "LEFT JOIN crm_people cp ON cp.id = fi.person_id "
                "WHERE fi.tenant_id = %s AND fi.face_label = %s LIMIT 1",
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


def _resolve_generic(channel: str) -> Callable[[str, str], IdentityContext | None]:
    """A resolver for any channel whose identities live in ``user_channel_identities``.

    A factory rather than one function taking the channel, because
    ``_RESOLVERS`` maps a name to a ``(identifier, tenant_id)`` callable and
    that shape is what ``resolve_identity`` caches against. Every channel that
    is not Telegram (whose source of truth is still ``tenant_users``, see
    ``_resolve_telegram``) and not webchat or vision resolves through here, so a
    plugin channel gets identity resolution by having rows rather than by
    shipping code.

    ``role`` comes off the BINDING, not off a joined account. A binding may
    legitimately name a user that neither ``user_accounts`` nor ``tenant_users``
    has a row for, and answering that with a default role would be the platform
    fabricating an authorization out of a query that returned nothing. The join
    is only for the things it is safe to be missing: a display name, an email,
    a ``person_id``.

    ``verified`` is True: unlike a vision face match, a binding is a row an
    operator wrote on purpose.
    """

    def _resolve(identifier: str, tenant_id: str) -> IdentityContext | None:
        try:
            with get_connection() as conn:
                cur = conn.cursor(cursor_factory=RealDictCursor)
                cur.execute("SELECT to_regclass('public.user_channel_identities') AS t")
                probe = cur.fetchone()
                if not probe or probe["t"] is None:
                    return None
                cur.execute(
                    "SELECT uci.user_id, uci.role, "
                    "COALESCE(NULLIF(uci.display_name, ''), "
                    "         NULLIF(ua.display_name, ''), "
                    "         NULLIF(tu.display_name, ''), '') AS display_name, "
                    "ua.id::TEXT AS user_account_id, ua.email, "
                    "COALESCE(ua.person_id::TEXT, tu.person_id::TEXT) AS person_id, "
                    "tu.user_id AS tenant_user_id "
                    "FROM user_channel_identities uci "
                    # The cast goes on the TEXT side, not on `ua.id`:
                    # `ua.id::TEXT = uci.user_id` casts an indexed UUID primary
                    # key on every inbound resolve, which the index cannot
                    # serve.
                    #
                    # It has to be a CASE and not `uci.user_id ~ %s AND
                    # ua.id = uci.user_id::UUID`, because Postgres does not
                    # promise to evaluate AND operands left to right -- and
                    # `user_id` legitimately holds non-UUID values (a
                    # `tenant_users.user_id`), so an eagerly-evaluated cast
                    # would raise InvalidTextRepresentation on a valid row and
                    # take the whole resolve down. CASE *is* documented to
                    # short-circuit.
                    "LEFT JOIN user_accounts ua "
                    "  ON ua.tenant_id = uci.tenant_id "
                    "  AND ua.id = (CASE WHEN uci.user_id ~ %s "
                    "                    THEN uci.user_id END)::UUID "
                    "LEFT JOIN tenant_users tu "
                    "  ON tu.tenant_id = uci.tenant_id AND tu.user_id = uci.user_id "
                    "WHERE uci.tenant_id = %s AND uci.channel = %s "
                    "  AND uci.native_id = %s AND uci.revoked_at IS NULL "
                    "LIMIT 1",
                    (_UUID_TEXT, tenant_id, channel, identifier),
                )
                match = cur.fetchone()
        except (psycopg2.errors.UndefinedTable, psycopg2.Error):
            logger.debug(
                "_resolve_generic: user_channel_identities unavailable for channel %r",
                channel,
                exc_info=True,
            )
            return None

        if not match:
            return None
        return IdentityContext(
            tenant_id=tenant_id,
            channel=channel,
            identifier=identifier,
            verified=True,
            display_name=match["display_name"] or "",
            role=match["role"] or "",
            tenant_user_id=match["tenant_user_id"],
            user_account_id=match["user_account_id"],
            person_id=match["person_id"],
            email=match["email"],
        )

    return _resolve


_RESOLVERS: dict[str, Callable[[str, str], IdentityContext | None]] = {
    "webchat": _resolve_webchat,
    "telegram": _resolve_telegram,
    "vision": _resolve_vision,
    "slack": _resolve_generic("slack"),
}


def resolver_for(channel: str) -> Callable[[str, str], IdentityContext | None]:
    """The resolver ``channel`` uses, registering a generic one if it has none.

    ``_RESOLVERS`` was a closed set of three, so every channel outside it —
    every plugin channel, and Slack until this release — resolved to ``None``
    for the life of the process no matter how many identities had been paired
    to it. A channel now earns resolution by having rows.
    """
    existing = _RESOLVERS.get(channel)
    if existing is not None:
        return existing
    created = _resolve_generic(channel)
    _RESOLVERS[channel] = created
    return created
