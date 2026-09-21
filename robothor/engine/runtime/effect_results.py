"""Retain acknowledged tool results without inventing independent verification."""

from psycopg2.extras import Json

from robothor.engine.runtime import effects


def record(context, effect_id, run_id, result):
    """Only the dispatched owner can atomically store a returned response."""
    with effects.get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE agent_runtime_effects SET state='finished',resolution=%s,
               version=version+1,updated_at=now()
               WHERE id=%s AND tenant_id=%s AND principal_id=%s AND request_id=%s
                 AND run_id=%s AND state='dispatching'""",
            (
                Json({"source": "tool_response", "result": result}),
                effect_id,
                context.tenant_id,
                context.principal_id,
                context.request_id,
                run_id,
            ),
        )
        return cur.rowcount == 1


def cacheable(result):
    return (
        isinstance(result, dict)
        and not result.get("error")
        and result.get("success") is not False
        and result.get("ok") is not False
        and result.get("status") not in ("error", "failed", "rejected")
    )
