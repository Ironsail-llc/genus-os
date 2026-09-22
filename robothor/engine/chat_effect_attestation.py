"""Explicit operator settlement of an uncertain chat action, with an audit note."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from robothor.auth.deps import AuthContext
from robothor.engine.chat_recovery import read_outcome
from robothor.engine.runtime import ExecutionContext, effects

router = APIRouter()


def reconcile(auth: AuthContext, session_key: str, body: dict[str, Any]) -> bool:
    if auth.role not in {"owner", "admin"} or auth.typ != "user":
        raise HTTPException(status_code=403, detail="An authenticated operator is required")
    try:
        effect_id = str(UUID(str(body["effect_id"])))
        request_id = str(UUID(str(body["request_id"])))
        note = body["note"]
        if not isinstance(note, str) or not note.strip() or len(note) > 4000:
            raise ValueError("audit note required")
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail="Effect, request and audit note required"
        ) from exc
    outcome = read_outcome(auth, session_key, request_id)
    if not outcome.get("terminal") or not any(
        item.get("kind") == "runtime_effect"
        and item.get("operation_id") == effect_id
        and item.get("status") == "uncertain"
        for item in outcome.get("effects", [])
    ):
        raise HTTPException(status_code=409, detail="No uncertain action in this completed request")
    context = ExecutionContext(auth.tenant_id, auth.user_id, request_id)
    return effects.attest(context, effect_id, actor=auth.user_id, note=note.strip())


@router.post("/reconcile-effect")
async def reconcile_effect(request: Request) -> JSONResponse:
    from robothor.engine.chat import _auth_context, _effective_session_key

    auth = _auth_context(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected an object")
    session = _effective_session_key(auth, body.get("session_key", ""))
    if not await asyncio.to_thread(reconcile, auth, session, body):
        raise HTTPException(
            status_code=409, detail="Action changed or belongs to a goal; refresh it"
        )
    return JSONResponse(
        {"reconciled": True, "verified": False}, headers={"Cache-Control": "no-store"}
    )
