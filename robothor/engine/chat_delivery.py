"""Use saved action evidence when the initial chat execution is interrupted."""

import asyncio
import logging
from types import SimpleNamespace
from uuid import UUID

from psycopg2.extras import RealDictCursor

from robothor.db.connection import get_connection
from robothor.engine.chat_effect_receipts import family_effect_receipts
from robothor.engine.chat_receipts import family_calendar_receipts
from robothor.engine.chat_result import receipt_result_text, result_text
from robothor.engine.models import RunStatus

logger = logging.getLogger(__name__)
_INTERRUPTED = {RunStatus.FAILED, RunStatus.TIMEOUT, RunStatus.CANCELLED}


def _saved_result(run_id, auth):
    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SET LOCAL statement_timeout = '750ms'")
        cur.execute(
            """SELECT id,agent_id,status,output_text,error_message FROM agent_runs
               WHERE id=%s AND tenant_id=%s AND user_id=%s""",
            (run_id, auth.tenant_id, auth.user_id),
        )
        row = cur.fetchone()
        if not row or RunStatus(row["status"]) not in _INTERRUPTED:
            return None
        receipts = family_calendar_receipts(cur, row, auth)
        receipts += family_effect_receipts(cur, row, auth)
    return {
        "text": receipt_result_text(
            SimpleNamespace(**{**row, "status": RunStatus(row["status"])}), receipts
        ),
        "audit_outcome": bool(receipts)
        and not any(
            item.get("reconciliation_pending") or item["status"] == "executing" for item in receipts
        ),
        "run_id": str(row["id"]),
        "status": row["status"],
    }


async def final_result(run, auth):
    fallback = result_text(run)
    if run.status not in _INTERRUPTED:
        return {"text": fallback}
    try:
        identifier = str(UUID(str(run.id)))
        async with asyncio.timeout(1):
            saved = await asyncio.to_thread(_saved_result, identifier, auth)
            return saved or {"text": fallback}
    except Exception as exc:
        # A failed audit read is not evidence of either success or nonapplication.
        logger.warning("Chat result audit unavailable: %s", type(exc).__name__)
        return {"text": fallback}


def _saved_request(identifier, auth):
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '750ms'")
        cur.execute(
            """SELECT id FROM agent_runs WHERE tenant_id=%s AND user_id=%s
               AND parent_run_id IS NULL
               AND (correlation_id=%s::uuid OR runtime_context->>'request_id'=%s)
               ORDER BY started_at DESC LIMIT 2""",
            (auth.tenant_id, auth.user_id, identifier, identifier),
        )
        rows = cur.fetchall()
    return _saved_result(str(rows[0][0]), auth) if len(rows) == 1 else None


async def deliver_interruption(
    queue, session, auth, session_key, message, *, aborted=False, plan=None
):
    """Recover an exception after the native runner durably recorded interruption."""
    from robothor.engine.chat_history import append_turn
    from robothor.engine.chat_store import save_exchange_async
    from robothor.engine.runtime.current import active_context

    context = active_context.get()
    if not context or (context.tenant_id, context.principal_id) != (auth.tenant_id, auth.user_id):
        return False
    try:
        async with asyncio.timeout(1):
            saved = await asyncio.to_thread(_saved_request, context.request_id, auth)
    except Exception as exc:
        logger.warning("Interrupted chat audit unavailable: %s", type(exc).__name__)
        return False
    if not saved:
        return False
    if plan is not None:
        from robothor.engine.chat_plan_claim import finish_plan

        plan.execution_run_id = saved["run_id"]
        await finish_plan(session, plan, auth.tenant_id, session_key)
    append_turn(session, user_message=message, assistant_text=saved["text"])
    asyncio.create_task(
        save_exchange_async(
            session_key,
            message,
            saved["text"],
            channel="webchat",
            model_override=session.model_override,
            tenant_id=auth.tenant_id,
        )
    )
    await queue.put({"event": "done", "data": {**saved, "aborted": aborted}})
    return True


async def deliver_plan_interruption(queue, session, auth, session_key, plan, error):
    """Resolve interrupted approval delivery without admitting the plan again."""
    aborted = isinstance(error, asyncio.CancelledError)
    if await deliver_interruption(
        queue, session, auth, session_key, plan.original_message, aborted=aborted, plan=plan
    ):
        return
    if aborted:
        await queue.put({"event": "done", "data": {"text": "", "aborted": True}})
    else:
        logger.error(
            "Plan execution error: %s",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        await queue.put({"event": "error", "data": {"error": str(error)}})
