"""Consume a saved plan once before admitting its execution."""

import asyncio

from robothor.db.connection import get_connection


def claim(tenant_id, session_key, plan, request_id):
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE chat_sessions SET
                plan_state=jsonb_set(jsonb_set(plan_state,'{status}','"approved"'::jsonb),
                                     '{approval_request_id}',to_jsonb(%s::text)),
                last_active_at=now()
               WHERE tenant_id=%s AND session_key=%s
                 AND plan_state->>'plan_id'=%s AND plan_state->>'status'='pending'
                 AND plan_state->>'plan_text'=%s AND plan_state->>'original_message'=%s
                 AND COALESCE(plan_state->>'deep_plan','false')=%s
                 AND plan_state->>'created_at'=%s""",
            (
                request_id,
                tenant_id,
                session_key,
                plan.plan_id,
                plan.plan_text,
                plan.original_message,
                str(plan.deep_plan).lower(),
                plan.created_at,
            ),
        )
        claimed = cur.rowcount == 1
        conn.commit()
        return claimed


async def claim_plan(tenant_id, session_key, plan, request_id):
    return await asyncio.to_thread(claim, tenant_id, session_key, plan, request_id)


async def admit_plan(session, auth, session_key, client_id):
    from copy import copy
    from uuid import uuid4

    from robothor.engine.runtime.chat_control import request_key

    plan = copy(session.active_plan)
    client_id = client_id or str(uuid4())
    if plan.status != "pending" or not await claim_plan(
        auth.tenant_id, session_key, plan, request_key(auth, session_key, client_id)
    ):
        return None, client_id
    plan.status = "approved"
    if session.active_plan and session.active_plan.plan_id == plan.plan_id:
        session.active_plan.status = "approved"
    return plan, client_id


def approval_refusal(session, plan_id):
    from fastapi.responses import JSONResponse

    from robothor.engine.chat import _plan_is_expired

    if not session.active_plan or session.active_plan.plan_id != plan_id:
        return JSONResponse(
            {"error": "No matching pending plan", "request_admitted": False}, status_code=404
        )

    if _plan_is_expired(session.active_plan):
        session.active_plan.status = "expired"
        session.active_plan = None
        return JSONResponse({"error": "Plan expired", "request_admitted": False}, status_code=410)

    # Verify plan integrity — ensure plan wasn't modified between proposal and approval
    if session.active_plan.plan_hash:
        import hashlib

        current_hash = hashlib.sha256(session.active_plan.plan_text.encode()).hexdigest()[:16]
        if current_hash != session.active_plan.plan_hash:
            return JSONResponse(
                {"error": "Plan integrity check failed", "request_admitted": False}, status_code=409
            )

    return None
