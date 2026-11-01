"""Durable stop authority. A stop denies later dispatch; in-flight effects need reconciliation."""

from __future__ import annotations

import threading
import time
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.db.connection import get_connection, tenant_scope

#: How long a tool call may reuse the verdict its iteration's provider attempt
#: read. Every stop issued in this process invalidates at once (``issue`` and
#: ``issue_request`` bump the generation), so this bounds only a stop committed
#: by another process; the next provider attempt always reads afresh as well.
STOP_REUSE_SECONDS = 10.0

_stop_lock = threading.Lock()
_stop_generation = 0
#: ``(tenant, run_id, request_id)`` -> ``(stopped, generation, checked_at)``.
_stop_cache: dict[tuple[str, str, str | None], tuple[bool, int, float]] = {}
_STOP_CACHE_MAX = 4096


def _now() -> float:
    return time.monotonic()


def invalidate_stop_cache() -> None:
    """Forget every cached verdict: a stop was just committed in this process."""
    global _stop_generation
    with _stop_lock:
        _stop_generation += 1
        _stop_cache.clear()


def issue(tenant: str, run_id: str, action: str, note: str = "") -> dict[str, Any]:
    if action not in {"pause", "cancel"}:
        raise ValueError(
            "runtime control must be pause or cancel; goal steering uses its lifecycle"
        )
    with (
        tenant_scope(tenant),
        get_connection() as conn,
        conn.cursor(cursor_factory=RealDictCursor) as cur,
    ):
        cur.execute(
            "SELECT id FROM agent_runs WHERE tenant_id=%s AND id=%s FOR UPDATE", (tenant, run_id)
        )
        if not cur.fetchone():
            raise ValueError("run not found in tenant")
        cur.execute(
            """INSERT INTO agent_runtime_controls(tenant_id,run_id,action,note)
                    VALUES (%s,%s,%s,%s) ON CONFLICT (tenant_id,run_id) DO UPDATE
                    SET action=CASE WHEN agent_runtime_controls.action='cancel' THEN 'cancel' ELSE EXCLUDED.action END,
                        note=EXCLUDED.note, version=agent_runtime_controls.version+1
                    RETURNING version,action""",
            (tenant, run_id, action, note),
        )
        result = dict(cur.fetchone())
    invalidate_stop_cache()
    signal_stopped(tenant, note)
    return {
        **result,
        "status": "stopping",
        "external_effects": "in-flight requests may finish; reconcile receipts",
    }


def issue_request(tenant: str, request_id: str, note: str = "") -> None:
    """Trusted host identity binds this stop, including pre-admission requests."""
    if not tenant or not request_id:
        raise ValueError("tenant and request identity required")
    with tenant_scope(tenant), get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO agent_runtime_request_stops(tenant_id,request_id,note)
               VALUES (%s,%s,%s) ON CONFLICT (tenant_id,request_id) DO NOTHING""",
            (tenant, request_id, note),
        )
    invalidate_stop_cache()
    signal_stopped(tenant, note)


def signal_stopped(tenant: str, note: str) -> None:
    # A local signal is an optimization. The database is authoritative for every descendant.
    from robothor.engine.runtime.activity import runs, stop_local
    from robothor.engine.session_registry import active_run_ids, lookup

    for active_id in set(active_run_ids()) | set(runs(tenant)):
        if stopped(tenant, active_id):
            stop_local(tenant, active_id, note or "Operator stopped execution")
    for active_id in active_run_ids():
        session = lookup(active_id)
        if session and session.run.tenant_id == tenant and stopped(tenant, active_id):
            session.interrupt(note or "Operator stopped execution")


def _own_request_id(tenant: str, run_id: str) -> str | None:
    from robothor.engine.runtime.activity import current
    from robothor.engine.runtime.current import active_context

    context, activity = active_context.get(), current.get()
    own_run = not run_id or (activity is not None and run_id in activity.sessions)
    return context.request_id if own_run and context and context.tenant_id == tenant else None


def stopped_for_iteration(tenant: str, run_id: str, *, refresh: bool = False) -> bool:
    """The durable-stop verdict, read once per loop iteration.

    ``refresh=True`` is the provider attempt that opens an iteration: it always
    reads the database and records the verdict. A tool call (``refresh=False``)
    reuses that verdict while no stop has been issued in this process since and
    it is younger than ``STOP_REUSE_SECONDS``. A stopped verdict is kept for as
    long as it is cached: stops are never withdrawn.
    """
    key = (tenant, run_id, _own_request_id(tenant, run_id))
    with _stop_lock:
        generation = _stop_generation
        hit = _stop_cache.get(key)
        if (
            not refresh
            and hit is not None
            and hit[1] == generation
            and (hit[0] or _now() - hit[2] < STOP_REUSE_SECONDS)
        ):
            return hit[0]
    verdict = stopped(tenant, run_id)
    with _stop_lock:
        if generation == _stop_generation:
            if len(_stop_cache) >= _STOP_CACHE_MAX:
                _stop_cache.clear()
            _stop_cache[key] = (verdict, generation, _now())
    return verdict


def stopped(tenant: str, run_id: str) -> bool:
    request_id = _own_request_id(tenant, run_id)
    with tenant_scope(tenant), get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """WITH RECURSIVE family AS (
            SELECT id,parent_run_id,runtime_context FROM agent_runs WHERE tenant_id=%s AND id=%s
            UNION
            SELECT r.id,r.parent_run_id,r.runtime_context FROM agent_runs r JOIN family f
              ON r.id IN (f.parent_run_id, CASE
                WHEN f.runtime_context->>'resume_from_run_id' ~
                  '^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$'
                THEN (f.runtime_context->>'resume_from_run_id')::uuid END)
            WHERE r.tenant_id=%s
        ) SELECT 1 FROM agent_runtime_controls c JOIN family f ON f.id=c.run_id
          WHERE c.tenant_id=%s
          UNION ALL
          SELECT 1 FROM agent_runtime_request_stops c WHERE c.tenant_id=%s AND
          (c.request_id=%s OR c.request_id IN (SELECT runtime_context->>'request_id' FROM family))
          LIMIT 1""",
            (tenant, run_id or None, tenant, tenant, tenant, request_id),
        )
        return cur.fetchone() is not None
