"""``PluginState`` — durable, tenant-scoped storage for plugins.

A plugin wheel cannot ship a migration, so a plugin that needs a "table" gets
a row *kind* in ``plugin_state`` (migration 154) instead: one JSON object per
``(tenant_id, plugin, kind, key)``. Two plugins, and two tenants, can use the
same kind and key without ever seeing each other's rows.

    from robothor.plugins.state import PluginState

    state = PluginState("acme")
    state.put("contact", "alice", {"stage": "lead"})
    state.put("seen", message_id, {}, ttl_seconds=86_400)
    state.get("contact", "alice")             # {"stage": "lead"}
    state.list("contact", prefix="a", limit=10)

A row written with ``ttl_seconds`` is invisible to ``get``, ``exists`` and
``list`` the moment it expires; the daemon deletes expired rows hourly
(:func:`purge_expired`) for the instance's tenant; rows for any other tenant
are deleted by :meth:`PluginState.purge_expired` on a store for that tenant.

Synchronous, on the platform pool and bound to the tenant for row-level
security, like the CRM DAL. From an async tool handler, call it through
``asyncio.to_thread`` — a blocking query must never run on the engine loop.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from psycopg2.extras import Json

from robothor.constants import DEFAULT_TENANT

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

__all__ = ["MAX_LIST_LIMIT", "MAX_TTL_SECONDS", "PluginState", "purge_expired"]

#: The most rows one ``list`` call returns.
MAX_LIST_LIMIT = 1000

#: The longest TTL ``put`` accepts: the database computes the expiry from an
#: ``int`` (32-bit), and a larger value would fail there instead of here.
MAX_TTL_SECONDS = 2**31 - 1

_LIVE = "(expires_at IS NULL OR expires_at > now())"


def _platform_connect(tenant_id: str) -> Callable[[], Any]:
    """The platform pool, bound to ``tenant_id`` for row-level security."""
    from robothor.db.connection import get_connection, tenant_scope

    @contextlib.contextmanager
    def connect() -> Iterator[Any]:
        with tenant_scope(tenant_id), get_connection() as conn:
            yield conn

    return connect


def _name(value: str, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"plugin state needs a non-empty {what}")
    return value


class PluginState:
    """One plugin's rows for one tenant.

    ``connect`` (tests) is a zero-argument callable returning a context manager
    that yields a psycopg2 connection and commits on a clean exit; by default
    the platform pool bound to ``tenant_id``.
    """

    def __init__(
        self,
        plugin: str,
        tenant_id: str = DEFAULT_TENANT,
        *,
        connect: Callable[[], Any] | None = None,
    ) -> None:
        self.plugin = _name(plugin, "plugin name")
        self.tenant_id = _name(tenant_id, "tenant id")
        self._connect_override = connect

    def _connect(self) -> Any:
        if self._connect_override is not None:
            return self._connect_override()
        return _platform_connect(self.tenant_id)()

    def _scope(self, kind: str, key: str) -> tuple[str, str, str, str]:
        return (self.tenant_id, self.plugin, _name(kind, "kind"), _name(key, "key"))

    def get(self, kind: str, key: str) -> dict[str, Any] | None:
        """The stored object, or ``None`` when there is none or it has expired."""
        params = self._scope(kind, key)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM plugin_state WHERE tenant_id = %s AND plugin = %s "
                f"AND kind = %s AND key = %s AND {_LIVE}",
                params,
            )
            row = cur.fetchone()
        return None if row is None else dict(row[0])

    def exists(self, kind: str, key: str) -> bool:
        """Whether a live (unexpired) row is stored under ``kind``/``key``."""
        params = self._scope(kind, key)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM plugin_state WHERE tenant_id = %s AND plugin = %s "
                f"AND kind = %s AND key = %s AND {_LIVE}",
                params,
            )
            return cur.fetchone() is not None

    def put(
        self,
        kind: str,
        key: str,
        value: dict[str, Any],
        ttl_seconds: int | None = None,
    ) -> None:
        """Store ``value``, replacing what was there.

        ``ttl_seconds`` makes the row expire that long from now; ``None`` keeps
        it until it is deleted. Re-putting a row resets its expiry.
        """
        params = self._scope(kind, key)
        if not isinstance(value, dict):
            raise TypeError(f"plugin state values are JSON objects, not {type(value).__name__}")
        if ttl_seconds is not None and (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not 0 < ttl_seconds <= MAX_TTL_SECONDS
        ):
            raise ValueError(
                f"ttl_seconds must be a whole number of seconds from 1 to {MAX_TTL_SECONDS}, "
                "or None"
            )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO plugin_state (tenant_id, plugin, kind, key, value, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, "
                "CASE WHEN %s::int IS NULL THEN NULL "
                "ELSE now() + make_interval(secs => %s::int) END) "
                "ON CONFLICT (tenant_id, plugin, kind, key) DO UPDATE SET "
                "value = EXCLUDED.value, expires_at = EXCLUDED.expires_at, "
                # An expired row nobody purged yet is a new row, not an old one.
                "created_at = CASE WHEN plugin_state.expires_at <= now() THEN now() "
                "ELSE plugin_state.created_at END, "
                "updated_at = now()",
                (*params, Json(value), ttl_seconds, ttl_seconds),
            )

    def delete(self, kind: str, key: str) -> bool:
        """Remove the row. ``True`` when a live row was removed."""
        params = self._scope(kind, key)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM plugin_state WHERE tenant_id = %s AND plugin = %s "
                f"AND kind = %s AND key = %s RETURNING {_LIVE}",
                params,
            )
            row = cur.fetchone()
        return bool(row and row[0])

    def list(self, kind: str, *, prefix: str = "", limit: int = 100) -> list[dict[str, Any]]:
        """Live rows of ``kind`` whose key starts with ``prefix``, in key order.

        Each row is ``{"key", "value", "updated_at", "expires_at"}``. The prefix
        is literal text: ``%`` and ``_`` are not wildcards. At most ``limit``
        rows, capped at :data:`MAX_LIST_LIMIT`.
        """
        _name(kind, "kind")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a whole number of rows, at least 1")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT key, value, updated_at, expires_at FROM plugin_state "
                "WHERE tenant_id = %s AND plugin = %s AND kind = %s "
                f"AND starts_with(key, %s) AND {_LIVE} ORDER BY key LIMIT %s",
                (self.tenant_id, self.plugin, kind, prefix or "", min(limit, MAX_LIST_LIMIT)),
            )
            rows = cur.fetchall()
        return [
            {"key": r[0], "value": dict(r[1]), "updated_at": r[2], "expires_at": r[3]} for r in rows
        ]

    def purge_expired(self) -> int:
        """Delete this plugin's expired rows for this tenant. Returns how many."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM plugin_state WHERE tenant_id = %s AND plugin = %s "
                "AND expires_at <= now()",
                (self.tenant_id, self.plugin),
            )
            return int(cur.rowcount or 0)


def purge_expired(
    tenant_id: str = DEFAULT_TENANT,
    *,
    connect: Callable[[], Any] | None = None,
) -> int:
    """Delete every plugin's expired rows for ``tenant_id``. Returns how many.

    The daemon's hourly sweep. Raises whatever the database raises; the caller
    decides what a failed sweep means.
    """
    _name(tenant_id, "tenant id")
    opener = connect or _platform_connect(tenant_id)
    with opener() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM plugin_state WHERE tenant_id = %s AND expires_at <= now()",
            (tenant_id,),
        )
        return int(cur.rowcount or 0)
