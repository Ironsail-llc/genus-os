"""Recover a chat delivery from durable run records without executing the request."""

from types import SimpleNamespace

from psycopg2.extras import RealDictCursor

from robothor.db.connection import get_connection
from robothor.engine.chat_receipts import calendar_receipts, receipt_summary
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
        receipts = calendar_receipts(cur, rows[0], auth) if len(rows) == 1 else []
    if not rows:
        return {"state": "not_found", "terminal": False}
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
    text = result_text(SimpleNamespace(**{**row, "status": status})) if terminal else ""
    incomplete = any(not item["verified"] and item["status"] != "draft" for item in receipts)
    if terminal and receipts:
        if status == RunStatus.COMPLETED and incomplete:
            text = "The run ended, but its recorded calendar action is not fully verified."
        text = "\n\n".join(filter(None, [text, receipt_summary(receipts)]))
    return {
        "state": status.value,
        "terminal": terminal,
        "run_id": str(row["id"]),
        "text": text,
        "effects": receipts,
        "verified": row["verified_status"] == "verified" and not incomplete,
        "source": "run_record",
    }
