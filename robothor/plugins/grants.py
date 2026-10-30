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
import logging
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

logger = logging.getLogger(__name__)

__all__ = [
    "GRANT_STATES",
    "ROOT_DISABLED_REASON",
    "clear_grant",
    "forced_states",
    "get_grants",
    "reset_forced_cache",
    "set_grant",
]

#: What a grant may say. ``None`` (no row) is the third state: not forced.
GRANT_STATES = ("on", "off")

#: The loader's refusal for a plugin the root tenant forced off. Beside the
#: lockfile's ``DISABLED_REASON`` ("disabled by operator"), and deliberately
#: different: the child's operator did not make this decision and cannot undo it.
ROOT_DISABLED_REASON = "disabled by the root tenant"


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


# ─── the engine's view: its own tenant's grants, once per generation ──────


def _read(tenant_id: str) -> dict[str, str]:
    """The seam the engine reads through; the suite replaces it."""
    return get_grants(tenant_id)


def _engine_tenant() -> str:
    """The tenant this process runs as. Read at call time, not import time."""
    from robothor import constants

    return constants.DEFAULT_TENANT


_lock = threading.Lock()
#: ``(generation, tenant, forced)`` from the newest read, successful or not.
_cache: tuple[int, str, dict[str, str]] | None = None
#: Per tenant, what the last SUCCESSFUL read said. What a failed read falls back on.
_last_good: dict[str, dict[str, str]] = {}


def reset_forced_cache() -> None:
    """Forget every read. For tests, and for nothing else."""
    global _cache
    with _lock:
        _cache = None
        _last_good.clear()


def forced_states(tenant_id: str | None = None) -> dict[str, str]:
    """``{plugin: "on" | "off"}`` the root tenant forced for this engine's tenant.

    Read once per plugin-registry generation (``loader.generation()``), so the
    loader may ask on every ``load_plugins`` call without a database read per
    call; a reload bumps the generation and the next ask reads again. That is
    why the bridge reloads a child's engine after writing a grant.

    **Fail closed.** When the read errors, every plugin that was forced ``off``
    at the last successful read stays refused, and the error is logged. A
    forced ``on`` is dropped instead, so the plugin falls back to the child's
    own lockfile: losing an override is the safe direction, losing a refusal
    is not. With no earlier successful read in this process there is nothing
    to fall back on and nothing is forced; the error is still logged, and the
    next reload reads again. The failed answer is kept for the rest of the
    generation, so an outage costs one read per generation, not one per load.
    """
    global _cache
    from robothor.plugins.loader import generation

    tenant = tenant_id or _engine_tenant()
    gen = generation()
    with _lock:
        if _cache is not None and _cache[0] == gen and _cache[1] == tenant:
            return dict(_cache[2])
    try:
        fresh = {name: state for name, state in _read(tenant).items() if state in GRANT_STATES}
    except Exception as exc:  # noqa: BLE001 - governance must never block boot
        with _lock:
            kept = {
                name: "off" for name, state in _last_good.get(tenant, {}).items() if state == "off"
            }
            _cache = (gen, tenant, kept)
        logger.warning(
            "Plugin grants for this tenant could not be read (%s); keeping %d plugin(s) "
            "forced off from the last successful read",
            type(exc).__name__,
            len(kept),
        )
        return dict(kept)
    with _lock:
        _last_good[tenant] = dict(fresh)
        _cache = (gen, tenant, fresh)
    return dict(fresh)
