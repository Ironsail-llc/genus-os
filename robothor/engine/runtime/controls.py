"""Durable stop authority. A stop denies later dispatch; in-flight effects need reconciliation."""

from __future__ import annotations

from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.db.connection import get_connection, tenant_scope


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


def stopped(tenant: str, run_id: str) -> bool:
    from robothor.engine.runtime.activity import current
    from robothor.engine.runtime.current import active_context

    context, activity = active_context.get(), current.get()
    own_run = not run_id or (activity is not None and run_id in activity.sessions)
    request_id = context.request_id if own_run and context and context.tenant_id == tenant else None
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
