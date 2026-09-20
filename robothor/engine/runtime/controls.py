"""Durable stop authority. A stop denies later dispatch; in-flight effects need reconciliation."""

from __future__ import annotations

from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.db.connection import get_connection


def issue(tenant: str, run_id: str, action: str, note: str = "") -> dict[str, Any]:
    if action not in {"pause", "cancel"}:
        raise ValueError(
            "runtime control must be pause or cancel; goal steering uses its lifecycle"
        )
    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
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
    return {
        **result,
        "status": "stopping",
        "external_effects": "in-flight requests may finish; reconcile receipts",
    }


def stopped(tenant: str, run_id: str) -> bool:
    if not run_id:
        return False
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """WITH RECURSIVE family AS (
            SELECT id,parent_run_id FROM agent_runs WHERE tenant_id=%s AND id=%s
            UNION
            SELECT r.id,r.parent_run_id FROM agent_runs r JOIN family f ON r.id=f.parent_run_id
            WHERE r.tenant_id=%s
        ) SELECT 1 FROM agent_runtime_controls c JOIN family f ON f.id=c.run_id
          WHERE c.tenant_id=%s LIMIT 1""",
            (tenant, run_id, tenant, tenant),
        )
        return cur.fetchone() is not None
