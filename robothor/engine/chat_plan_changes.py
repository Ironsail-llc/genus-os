"""Change only the pending draft that the operator actually reviewed."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    from datetime import datetime

    from fastapi.responses import JSONResponse

    from robothor.auth.deps import AuthContext
    from robothor.engine.chat import ChatSession
    from robothor.engine.models import AgentRun, PlanState

import asyncio
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from fastapi.responses import JSONResponse
from psycopg2.extras import Json

from robothor.db.connection import get_connection, tenant_scope
from robothor.engine.chat_plan_claim import approval_refusal
from robothor.engine.chat_result import result_text
from robothor.engine.models import RunStatus


def replace_pending(
    tenant_id: str, session_key: str, expected: PlanState, replacement: PlanState | None
) -> bool:
    with tenant_scope(tenant_id), get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE chat_sessions SET plan_state=%s,last_active_at=now()
               WHERE tenant_id=%s AND session_key=%s
                 AND plan_state->>'plan_id'=%s AND plan_state->>'status'='pending'
                 AND plan_state->>'plan_text'=%s AND plan_state->>'original_message'=%s
                 AND COALESCE(plan_state->>'deep_plan','false')=%s
                 AND plan_state->>'created_at'=%s""",
            (
                Json(asdict(replacement)) if replacement else None,
                tenant_id,
                session_key,
                expected.plan_id,
                expected.plan_text,
                expected.original_message,
                str(expected.deep_plan).lower(),
                expected.created_at,
            ),
        )
        changed = cur.rowcount == 1
        conn.commit()
        return bool(changed)


async def replace_pending_async(
    tenant_id: str, session_key: str, expected: PlanState, replacement: PlanState | None
) -> bool:
    return await asyncio.to_thread(replace_pending, tenant_id, session_key, expected, replacement)


async def reject_plan(
    session: ChatSession, auth: AuthContext, session_key: str, plan_id: str, feedback: str
) -> JSONResponse:
    from robothor.secrets.redaction import redact

    if refusal := approval_refusal(session, plan_id):
        return refusal
    original = session.active_plan
    if original is None:
        return JSONResponse({"error": "No matching pending plan"}, status_code=404)
    expected = deepcopy(original)
    if not await replace_pending_async(auth.tenant_id, session_key, expected, None):
        return JSONResponse(
            {"error": "This draft is no longer pending.", "request_admitted": False},
            status_code=409,
        )
    if session.active_plan is original:
        session.active_plan = None
    if feedback:
        session.history.append(
            {
                "role": "system",
                "content": redact(
                    f"[PLAN REJECTED] The previous plan was rejected. Feedback: {feedback}"
                ),
            }
        )
    return JSONResponse({"ok": True})


async def revise_plan(
    session: ChatSession,
    original: PlanState,
    expected: PlanState,
    run: AgentRun,
    feedback: str,
    tenant_id: str,
    session_key: str,
) -> tuple[PlanState | None, str]:
    from robothor.engine.chat import _extract_plan_text, _plan_is_expired
    from robothor.secrets.redaction import redact

    output = result_text(run)
    text = _extract_plan_text(output) if run.status == RunStatus.COMPLETED else ""
    if not text:
        return None, output
    revised = deepcopy(expected)
    revised.plan_id = str(uuid4())
    revised.created_at = datetime.now(UTC).isoformat()
    revised.plan_text = text
    revised.plan_hash = sha256(text.encode()).hexdigest()[:16]
    revised.exploration_run_id = run.id
    revised.approval_request_id = ""
    revised.execution_run_id = ""
    revised.revision_count += 1
    revised.revision_history.append(
        {
            "plan_text": expected.plan_text,
            "feedback": redact(feedback),
            "timestamp": revised.created_at,
        }
    )
    if _plan_is_expired(expected) or not await replace_pending_async(
        tenant_id, session_key, expected, revised
    ):
        return None, "This draft is no longer pending. The revision was not applied."
    if session.active_plan is not original:
        return None, "The revision was recorded, but the current session has since changed."
    session.active_plan = revised
    return revised, output
