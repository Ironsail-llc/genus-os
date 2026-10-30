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
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

logger = logging.getLogger(__name__)

__all__ = [
    "GRANT_STATES",
    "ROOT_DISABLED_REASON",
    "canonical",
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


def canonical(name: str) -> str:
    """A distribution name in PEP 503 form: ``Acme_Tools`` and ``acme-tools`` are one.

    Grants are written by a person and matched against the metadata ``Name``,
    so both sides go through this, or a grant of ``off`` would report "live"
    and force nothing.
    """
    from packaging.utils import canonicalize_name

    return str(canonicalize_name(name))


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
    return {canonical(str(plugin)): str(state) for plugin, state in rows}


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
    name = canonical(_name(plugin, "plugin name"))
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
    name = canonical(_name(plugin, "plugin name"))
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
#: Tenants whose read failed with nothing to fall back on. The registries built
#: meanwhile may hold a plugin the root forced off, so the first successful
#: read for one of these bumps the plugin generation to rebuild them.
_unknown_since_failure: set[str] = set()


def reset_forced_cache() -> None:
    """Forget every read. For tests, and for nothing else."""
    global _cache
    with _lock:
        _cache = None
        _last_good.clear()
        _unknown_since_failure.clear()


def _persisted_path() -> Path | None:
    """Beside the lockfile: the forced-off set of the last successful read."""
    from robothor.plugins.lockfile import lockfile_path

    lock = lockfile_path()
    return None if lock is None else lock.with_name(lock.name + ".forced-off.json")


def _load_persisted(tenant: str) -> set[str] | None:
    """The persisted forced-off set for ``tenant``, or None when there is none."""
    try:
        path = _persisted_path()
        if path is None:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 - an unreadable record is no record
        logger.warning("Persisted plugin grants could not be read (%s)", type(exc).__name__)
        return None
    if not isinstance(data, dict) or data.get("tenant") != tenant:
        return None
    names = data.get("forced_off")
    return {canonical(n) for n in names if isinstance(n, str)} if isinstance(names, list) else None


def _persist(tenant: str, forced_off: set[str]) -> None:
    """Record the forced-off set, so a restart with the database down keeps it."""
    try:
        path = _persisted_path()
        if path is None or _load_persisted(tenant) == forced_off:
            return
        if not forced_off and not path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".plugins.forced-off.")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                json.dump({"tenant": tenant, "forced_off": sorted(forced_off)}, fh)
            tmp.chmod(0o600)
            tmp.replace(path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    except Exception as exc:  # noqa: BLE001 - a record we could not keep is logged, not fatal
        logger.warning("Plugin grants could not be persisted (%s)", type(exc).__name__)


def forced_states(tenant_id: str | None = None) -> dict[str, str]:
    """``{plugin: "on" | "off"}`` the root tenant forced for this engine's tenant.

    Keys are canonical distribution names (:func:`canonical`). Read once per
    plugin-registry generation (``loader.generation()``), so the loader may ask
    on every ``load_plugins`` call without a database read per call; a reload
    bumps the generation and the next ask reads again. That is why the bridge
    reloads a child's engine after writing a grant.

    **Fail closed.** When the read errors, every plugin that was forced ``off``
    at the last successful read stays refused, and the error is logged. The
    last successful read is kept in memory and persisted beside the lockfile,
    so it survives a restart with the database down. A forced ``on`` is
    dropped instead, so the plugin falls back to the child's own lockfile:
    losing an override is the safe direction, losing a refusal is not.

    A failed read with a fallback is kept for the rest of the generation, so an
    outage costs one read per generation, not one per load. A failed read with
    NO fallback (no earlier read, nothing persisted) is not cached: the next
    load asks again, and the first read that then succeeds with anything forced
    off bumps the plugin generation, so the registries built in the meantime
    rebuild without it. (A module that was already imported has run; it is no
    longer registered.)
    """
    global _cache
    from robothor.plugins.loader import generation

    tenant = tenant_id or _engine_tenant()
    gen = generation()
    with _lock:
        if _cache is not None and _cache[0] == gen and _cache[1] == tenant:
            return dict(_cache[2])
    try:
        fresh = {
            canonical(name): state for name, state in _read(tenant).items() if state in GRANT_STATES
        }
    except Exception as exc:  # noqa: BLE001 - governance must never block boot
        with _lock:
            remembered = _last_good.get(tenant)
        if remembered is not None:
            off: set[str] | None = {n for n, state in remembered.items() if state == "off"}
        else:
            off = _load_persisted(tenant)
        known = off is not None
        kept = dict.fromkeys(off or (), "off")
        with _lock:
            if known:
                _cache = (gen, tenant, kept)
            else:
                _unknown_since_failure.add(tenant)
        logger.warning(
            "Plugin grants for this tenant could not be read (%s); keeping %d plugin(s) "
            "forced off from the last successful read%s",
            type(exc).__name__,
            len(kept),
            "" if known else "; none known yet, so the next load retries",
        )
        return dict(kept)
    forced_off = {name for name, state in fresh.items() if state == "off"}
    with _lock:
        recovering = tenant in _unknown_since_failure
        _unknown_since_failure.discard(tenant)
    if recovering and forced_off:
        # Whatever was built while the grants were unknown may hold a plugin
        # the root forced off. Mark every plugin cache stale; each rebuilds on
        # its next read, through this function, at the new generation.
        from robothor.plugins.loader import reload_plugins

        logger.warning(
            "Plugin grants readable again; reloading to apply %d refusal(s)", len(forced_off)
        )
        gen = reload_plugins()
    with _lock:
        _last_good[tenant] = dict(fresh)
        _cache = (gen, tenant, fresh)
    _persist(tenant, forced_off)
    return dict(fresh)
