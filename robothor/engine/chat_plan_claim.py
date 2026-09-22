"""Consume a saved plan once before admitting its execution."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio

    from fastapi.responses import JSONResponse

    from robothor.auth.deps import AuthContext
    from robothor.engine.chat import ChatSession
    from robothor.engine.models import PlanState

import asyncio

from robothor.db.connection import get_connection, tenant_scope
from robothor.engine.chat_approval_receipt import record_claim


def claim(tenant_id: str, session_key: str, plan: PlanState, request_id: str) -> bool:
    with tenant_scope(tenant_id), get_connection() as conn, conn.cursor() as cur:
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
        if claimed:
            claimed = record_claim(conn, cur, tenant_id, session_key, request_id)
        conn.commit()
        return bool(claimed)


async def claim_plan(tenant_id: str, session_key: str, plan: PlanState, request_id: str) -> bool:
    return await asyncio.to_thread(claim, tenant_id, session_key, plan, request_id)


async def admit_plan(
    session: ChatSession, auth: AuthContext, session_key: str, client_id: str | None
) -> tuple[PlanState | None, str]:
    from copy import copy
    from uuid import uuid4

    from robothor.engine.runtime.chat_control import request_key

    plan = copy(session.active_plan)
    client_id = client_id or str(uuid4())
    identifier = request_key(auth, session_key, client_id)
    if (
        plan is None
        or plan.status != "pending"
        or not await claim_plan(auth.tenant_id, session_key, plan, identifier)
    ):
        return None, client_id
    plan.status = "approved"
    plan.approval_request_id = identifier
    if session.active_plan and session.active_plan.plan_id == plan.plan_id:
        session.active_plan.status = "approved"
        session.active_plan.approval_request_id = identifier
    return plan, client_id


def approval_refusal(session: ChatSession, plan_id: str) -> JSONResponse | None:
    from fastapi.responses import JSONResponse

    from robothor.engine.chat import _plan_is_expired

    if not session.active_plan or session.active_plan.plan_id != plan_id:
        return JSONResponse(
            {"error": "No matching pending plan", "request_admitted": False}, status_code=404
        )

    if session.active_plan.status != "pending":
        return JSONResponse(
            {"error": "This plan is no longer pending.", "request_admitted": False}, status_code=409
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


def already_admitted(auth: AuthContext, session_key: str, client_id: str | None) -> bool:
    from robothor.engine.runtime.chat_control import request_key

    identifier = request_key(auth, session_key, client_id)
    with tenant_scope(auth.tenant_id), get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT EXISTS(
                SELECT 1 FROM agent_runs WHERE tenant_id=%s AND user_id=%s
                  AND parent_run_id IS NULL
                  AND (correlation_id=%s::uuid OR runtime_context->>'request_id'=%s)
            ) OR EXISTS(
                SELECT 1 FROM chat_sessions WHERE tenant_id=%s AND session_key=%s
                  AND plan_state->>'approval_request_id'=%s
            ) OR EXISTS(
                SELECT 1 FROM chat_approval_receipts WHERE tenant_id=%s
                  AND session_key=%s AND request_id=%s
            )""",
            (
                auth.tenant_id,
                auth.user_id,
                identifier,
                identifier,
                auth.tenant_id,
                session_key,
                identifier,
                auth.tenant_id,
                session_key,
                identifier,
            ),
        )
        return bool(cur.fetchone()[0])


async def approval_retry(
    auth: AuthContext, session_key: str, client_id: str | None
) -> JSONResponse | None:
    from fastapi.responses import JSONResponse

    if client_id is not None and await asyncio.to_thread(
        already_admitted, auth, session_key, client_id
    ):
        return JSONResponse(
            {
                "error": "That approval was already received. Checking its recorded result.",
                "request_admitted": True,
            },
            status_code=409,
        )
    return None


def clear_claim(tenant_id: str, session_key: str, plan_id: str, request_id: str) -> None:
    with tenant_scope(tenant_id), get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE chat_sessions SET plan_state=NULL,last_active_at=now()
               WHERE tenant_id=%s AND session_key=%s
                 AND plan_state->>'plan_id'=%s AND plan_state->>'status'='approved'
                 AND plan_state->>'approval_request_id'=%s""",
            (tenant_id, session_key, plan_id, request_id),
        )
        conn.commit()


async def finish_plan(
    session: ChatSession, plan: PlanState, tenant_id: str, session_key: str
) -> None:
    import logging

    current = session.active_plan
    if (
        current is not None
        and current.plan_id == plan.plan_id
        and current.status == "approved"
        and current.approval_request_id == plan.approval_request_id
    ):
        session.active_plan = None
    try:
        await asyncio.to_thread(
            clear_claim, tenant_id, session_key, plan.plan_id, plan.approval_request_id
        )
    except Exception as exc:
        # Keeping an approved claim prevents a duplicate execution after a store
        # outage. The run's durable outcome remains available for recovery.
        logging.getLogger(__name__).warning("Plan retirement deferred (%s)", type(exc).__name__)
