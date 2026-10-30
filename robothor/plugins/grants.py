"""The root tenant's per-child plugin decisions: ``tenant_plugin_grants``.

A root operator may force one plugin ``on`` or ``off`` for one child tenant
(migration 160). The child's engine reads its own rows; ``off`` is refused
before import and ``on`` loads even when the child's lockfile row says
disabled. No row means the child's own lockfile decides, exactly as before.

The grants live in a table of their own rather than in
``crm_tenants.settings``: a child admin may PATCH its tenant's settings, and a
save replaces the whole object, so a grant kept there would be one the child
could delete.

Synchronous, on the platform pool and bound to the tenant for row-level
security, like ``robothor.plugins.state``. ``connect`` is injectable for tests:
a zero-argument callable returning a context manager that yields a psycopg2
connection and commits on a clean exit.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

__all__ = ["GRANT_STATES", "clear_grant", "get_grants", "set_grant"]

#: What a grant may say. ``None`` (no row) is the third state: not forced.
GRANT_STATES = ("on", "off")


def _platform_connect(tenant_id: str) -> Callable[[], Any]:
    """The platform pool, bound to ``tenant_id`` for row-level security."""
    from robothor.db.connection import get_connection, tenant_scope

    @contextlib.contextmanager
    def connect() -> Iterator[Any]:
        with tenant_scope(tenant_id), get_connection() as conn:
            yield conn

    return connect


def _name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"a plugin grant needs a non-empty {what}")
    return value


def _connection(tenant_id: str, connect: Callable[[], Any] | None) -> Any:
    return (connect or _platform_connect(tenant_id))()


def get_grants(tenant_id: str, *, connect: Callable[[], Any] | None = None) -> dict[str, str]:
    """Every grant for ``tenant_id``, as ``{plugin: "on" | "off"}``.

    Raises on a database error. Deciding what an unreadable record means is
    the caller's job, and the loader's answer (fail closed) is not one this
    module should make for everybody.
    """
    tenant = _name(tenant_id, "tenant id")
    with _connection(tenant, connect) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT plugin, state FROM tenant_plugin_grants WHERE tenant_id = %s",
            (tenant,),
        )
        rows = cur.fetchall()
    return {str(plugin): str(state) for plugin, state in rows}


def set_grant(
    tenant_id: str,
    plugin: str,
    state: str,
    set_by: str,
    *,
    connect: Callable[[], Any] | None = None,
) -> None:
    """Force ``plugin`` to ``state`` for ``tenant_id``, replacing any earlier grant."""
    tenant = _name(tenant_id, "tenant id")
    name = _name(plugin, "plugin name")
    if not isinstance(state, str) or state not in GRANT_STATES:
        raise ValueError(f"a plugin grant's state is 'on' or 'off', not {state!r}")
    actor = _name(set_by, "actor")
    with _connection(tenant, connect) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenant_plugin_grants (tenant_id, plugin, state, set_by, set_at) "
            "VALUES (%s, %s, %s, %s, now()) "
            "ON CONFLICT (tenant_id, plugin) DO UPDATE "
            "SET state = EXCLUDED.state, set_by = EXCLUDED.set_by, set_at = now()",
            (tenant, name, state, actor),
        )


def clear_grant(tenant_id: str, plugin: str, *, connect: Callable[[], Any] | None = None) -> bool:
    """Drop the grant, so the child decides again. True when a row was removed."""
    tenant = _name(tenant_id, "tenant id")
    name = _name(plugin, "plugin name")
    with _connection(tenant, connect) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM tenant_plugin_grants WHERE tenant_id = %s AND plugin = %s",
            (tenant, name),
        )
        return bool(cur.rowcount)
