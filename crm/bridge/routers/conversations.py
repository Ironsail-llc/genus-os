"""Conversations & Messages routes."""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from robothor.crm.dal import (
    get_conversation,
    list_conversations,
    list_messages,
    send_message,
    toggle_conversation_status,
)

from models import CreateMessageRequest, ToggleStatusRequest

router = APIRouter(prefix="/api", tags=["conversations"])


@router.get("/conversations")
async def api_list_conversations(
    status: str = Query("open"),
    page: int = Query(1),
):
    return list_conversations(status, page)


@router.get("/conversations/{conversation_id}")
async def api_get_conversation(conversation_id: int):
    result = get_conversation(conversation_id)
    if not result:
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    return result


@router.get("/conversations/{conversation_id}/messages")
async def api_list_messages(conversation_id: int):
    result = list_messages(conversation_id)
    return {"payload": result}


@router.post("/conversations/{conversation_id}/messages")
async def api_create_message(conversation_id: int, body: CreateMessageRequest):
    if not body.content:
        return JSONResponse({"error": "content required"}, status_code=400)
    result = send_message(conversation_id, body.content, body.message_type, body.private)
    return result or {"status": "ok"}


@router.post("/conversations/{conversation_id}/toggle_status")
async def api_toggle_conversation_status(conversation_id: int, body: ToggleStatusRequest):
    if body.status not in ("open", "resolved", "pending", "snoozed"):
        return JSONResponse(
            {"error": "status must be open, resolved, pending, or snoozed"}, status_code=400,
        )
    result = toggle_conversation_status(conversation_id, body.status)
    if not result:
        return JSONResponse({"error": "failed to toggle status"}, status_code=500)
    return {"success": True, "status": body.status}
