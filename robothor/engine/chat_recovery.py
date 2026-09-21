"""Recover a chat delivery from durable run records without executing the request."""

from types import SimpleNamespace

from psycopg2.extras import RealDictCursor

from robothor.db.connection import get_connection
from robothor.engine.chat_continuation import continuation
from robothor.engine.chat_effect_receipts import family_effect_receipts
from robothor.engine.chat_receipts import family_calendar_receipts, receipt_summary
from robothor.engine.chat_result import result_text
from robothor.engine.models import RunStatus
from robothor.engine.runtime.chat_control import request_key


def read_outcome(auth, session_key: str, client_id: str) -> dict:
    identifier = request_key(auth, session_key, client_id)
    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """SELECT id,agent_id,status,output_text,error_message,verified_status FROM agent_runs
               WHERE tenant_id=%s AND user_id=%s AND parent_run_id IS NULL
                 AND (correlation_id=%s::uuid OR runtime_context->>'request_id'=%s)
               ORDER BY started_at DESC LIMIT 2""",
            (auth.tenant_id, auth.user_id, identifier, identifier),
        )
        rows = cur.fetchall()
        if not rows:
            from robothor.engine.chat_pending_outcome import pending_outcome

            return pending_outcome(cur, auth, session_key, identifier)
        receipts = []
        if len(rows) == 1:
            root = rows[0]
            latest = continuation(cur, root, auth)
            if latest is None:
                return {"state": "ambiguous", "terminal": False}
            rows = [latest]
            receipts = family_calendar_receipts(cur, root, auth)
            receipts += family_effect_receipts(cur, root, auth)
    if len(rows) != 1:
        return {"state": "ambiguous", "terminal": False}
    row = rows[0]
    status = RunStatus(row["status"])
    terminal = status in {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.TIMEOUT,
        RunStatus.CANCELLED,
        RunStatus.SKIPPED,
    }
    stop_requested = False
    if not terminal:
        from robothor.engine.runtime.controls import stopped

        stop_requested = stopped(auth.tenant_id, str(row["id"]))
    text = result_text(SimpleNamespace(**{**row, "status": status})) if terminal else ""
    if stop_requested:
        text = "Stop is recorded. Waiting for the original worker's final evidence."
    incomplete = any(
        not item["verified"] and item["status"] != "draft" and not item.get("superseded_by")
        for item in receipts
    )
    if terminal and receipts:
        if status == RunStatus.COMPLETED and incomplete:
            text = "The run ended, but a recorded action is not fully verified."
        text = "\n\n".join(filter(None, [text, receipt_summary(receipts)]))
    return {
        "state": "stopping" if stop_requested else status.value,
        "stop_requested": stop_requested,
        "terminal": terminal,
        "run_id": str(row["id"]),
        "agent_id": row["agent_id"],
        "text": text,
        "effects": receipts,
        "reconciliation_pending": any(
            item["status"] == "executing" or item.get("reconciliation_pending", False)
            for item in receipts
        ),
        "verified": status == RunStatus.COMPLETED
        and row["verified_status"] == "verified"
        and not incomplete,
        "source": "run_record",
        "plan_exploration": str(row.get("trigger_detail") or "").startswith(
            ("plan:", "plan-revise:")
        ),
    }
