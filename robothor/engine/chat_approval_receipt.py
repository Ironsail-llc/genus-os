"""Retain approval evidence in the same transaction that consumes a pending plan."""

from psycopg2.errors import UniqueViolation


def record_claim(conn, cur, tenant_id, session_key, request_id):
    try:
        cur.execute(
            """INSERT INTO chat_approval_receipts
               (tenant_id,request_id,session_key,plan_id,plan_state)
               SELECT tenant_id,%s,session_key,plan_state->>'plan_id',plan_state
               FROM chat_sessions WHERE tenant_id=%s AND session_key=%s""",
            (request_id, tenant_id, session_key),
        )
    except UniqueViolation:
        # Roll back the plan change too: a reused request or plan cannot consume
        # a new pending draft even if its in-memory cache was stale.
        conn.rollback()
        return False
    return True
