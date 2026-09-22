"""Retain acknowledged tool results without inventing independent verification."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from robothor.db.connection import tenant_scope

if TYPE_CHECKING:
    from robothor.engine.runtime.contracts import ExecutionContext

from psycopg2.extras import Json

from robothor.engine.runtime import effects


def record(context: ExecutionContext, effect_id: Any, run_id: str, result: Any) -> bool:
    """Only the dispatched owner can atomically store a returned response."""
    with tenant_scope(context.tenant_id), effects.get_connection() as conn, conn.cursor() as cur:
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
        return bool(cur.rowcount == 1)


def cacheable(result: Any) -> bool:
    return (
        isinstance(result, dict)
        and not result.get("error")
        and result.get("success") is not False
        and result.get("ok") is not False
        and result.get("status") not in ("error", "failed", "rejected")
    )
