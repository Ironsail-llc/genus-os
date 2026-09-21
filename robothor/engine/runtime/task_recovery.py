"""Trusted CRM task readback keyed by the pre-dispatch effect identity."""

import logging
from types import SimpleNamespace

from psycopg2.extras import RealDictCursor

from robothor.engine.runtime import effects

logger = logging.getLogger(__name__)


def task_options(ctx, args):
    record = effects.active_effect.get()
    if record is None:
        return {}
    if (
        record["tool_name"] != "create_task"
        or record["tenant_id"] != ctx.tenant_id
        or record["run_id"] != ctx.run_id
        or record["fingerprint"] != effects.fingerprint("create_task", args)
    ):
        raise ValueError("Task effect identity does not match the authorized dispatch")
    # Host-only opt-in: synthetic or adapter tools with this name are not verifiers.
    record["_native_readback"] = True
    return {"task_id": str(record["id"])}


def verify(record):
    # Either the reserved new ID or an existing ID recorded by trusted dedup code.
    lookup = (record.get("resolution") or {}).get("lookup") or {}
    identifier = str(lookup.get("task_id") or record["id"])
    with effects.get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT title FROM crm_tasks WHERE tenant_id=%s AND id=%s AND deleted_at IS NULL",
            (record["tenant_id"], identifier),
        )
        row = cur.fetchone()
    if row is None:
        return effects.Verification("unknown")
    return effects.Verification(
        "applied",
        True,
        "crm_tasks:" + identifier,
        {"id": identifier, "title": row[0], **({"deduplicated": True} if lookup else {})},
    )


def recover(context, effect_id):
    try:
        record = effects.read(context, effect_id)
        if not record or record["tool_name"] != "create_task":
            return None
        if record["state"] == "uncertain":
            effects.resolve(context, effect_id, verify)
            record = effects.read(context, effect_id)
        if record and record["state"] == "confirmed":
            return {
                **record["resolution"]["result"],
                "effect_id": str(effect_id),
                "recovered": True,
            }
    except Exception:
        logger.warning("CRM task readback deferred", exc_info=True)
    return None


def sweep(tenant_id):
    with effects.get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """SELECT id,principal_id FROM agent_runtime_effects WHERE tenant_id=%s
               AND tool_name='create_task' AND state='uncertain'
               ORDER BY updated_at,id LIMIT 100""",
            (tenant_id,),
        )
        rows = cur.fetchall()
    for row in rows:
        recover(SimpleNamespace(tenant_id=tenant_id, principal_id=row["principal_id"]), row["id"])
        # Rotate unresolved readbacks so a missing/deleted task cannot starve
        # later records. This changes no verdict or compare-and-swap version.
        with effects.get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_runtime_effects SET updated_at=now() WHERE id=%s AND tenant_id=%s AND state='uncertain'",
                (row["id"], tenant_id),
            )
