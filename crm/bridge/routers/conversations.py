"""Conversations & Messages routes."""

from __future__ import annotations

from deps import get_tenant_id
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from models import CreateMessageRequest, ToggleStatusRequest

from robothor.crm.dal import (
    get_conversation,
    list_conversations,
    list_messages,
    send_message,
    toggle_conversation_status,
)

router = APIRouter(prefix="/api", tags=["conversations"])


@router.get("/conversations")
async def api_list_conversations(
    status: str = Query("open"),
    page: int = Query(1),
    tenant_id: str = Depends(get_tenant_id),
):
    return list_conversations(status, page, tenant_id=tenant_id)


@router.get("/conversations/{conversation_id}")
async def api_get_conversation(
    conversation_id: int,
    tenant_id: str = Depends(get_tenant_id),
):
    result = get_conversation(conversation_id, tenant_id=tenant_id)
    if not result:
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    return result


@router.get("/conversations/{conversation_id}/messages")
async def api_list_messages(
    conversation_id: int,
    tenant_id: str = Depends(get_tenant_id),
):
    result = list_messages(conversation_id, tenant_id=tenant_id)
    return {"payload": result}


@router.post("/conversations/{conversation_id}/messages")
async def api_create_message(
    conversation_id: int,
    body: CreateMessageRequest,
    tenant_id: str = Depends(get_tenant_id),
):
    if not body.content:
        return JSONResponse({"error": "content required"}, status_code=400)
    result = send_message(
        conversation_id,
        body.content,
        body.message_type,
        body.private,
        tenant_id=tenant_id,
    )
    return result or {"status": "ok"}


@router.post("/conversations/{conversation_id}/toggle_status")
async def api_toggle_conversation_status(
    conversation_id: int,
    body: ToggleStatusRequest,
    tenant_id: str = Depends(get_tenant_id),
):
    if body.status not in ("open", "resolved", "pending", "snoozed"):
        return JSONResponse(
            {"error": "status must be open, resolved, pending, or snoozed"},
            status_code=400,
        )
    result = toggle_conversation_status(conversation_id, body.status, tenant_id=tenant_id)
    if not result:
        return JSONResponse({"error": "failed to toggle status"}, status_code=500)
    return {"success": True, "status": body.status}
