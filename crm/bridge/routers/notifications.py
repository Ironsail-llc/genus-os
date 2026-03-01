"""Agent-to-Agent Notification routes."""

from __future__ import annotations

from deps import get_tenant_id
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from models import SendNotificationRequest

from robothor.crm.dal import (
    acknowledge_notification,
    get_agent_inbox,
    list_notifications,
    mark_notification_read,
    send_notification,
)
from robothor.events.bus import publish

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.post("/send")
async def api_send_notification(
    body: SendNotificationRequest,
    tenant_id: str = Depends(get_tenant_id),
):
    if not body.subject:
        return JSONResponse({"error": "subject required"}, status_code=400)
    notification_id = send_notification(
        from_agent=body.fromAgent,
        to_agent=body.toAgent,
        notification_type=body.notificationType,
        subject=body.subject,
        body=body.body,
        metadata=body.metadata,
        task_id=body.taskId,
        tenant_id=tenant_id,
    )
    if notification_id:
        publish(
            "agent",
            "notification.sent",
            {
                "notification_id": notification_id,
                "from_agent": body.fromAgent,
                "to_agent": body.toAgent,
                "type": body.notificationType,
                "tenant_id": tenant_id,
            },
            source="bridge",
        )
        return {"id": notification_id, "subject": body.subject}
    return JSONResponse({"error": "failed to send notification"}, status_code=500)


@router.get("/inbox/{agent_id}")
async def api_get_inbox(
    agent_id: str,
    unreadOnly: bool = Query(True),
    typeFilter: str | None = Query(None),
    limit: int = Query(50),
    tenant_id: str = Depends(get_tenant_id),
):
    return {
        "notifications": get_agent_inbox(
            agent_id=agent_id,
            unread_only=unreadOnly,
            type_filter=typeFilter,
            limit=limit,
            tenant_id=tenant_id,
        )
    }


@router.post("/{notification_id}/read")
async def api_mark_read(
    notification_id: str,
    tenant_id: str = Depends(get_tenant_id),
):
    if mark_notification_read(notification_id, tenant_id=tenant_id):
        return {"success": True, "id": notification_id}
    return JSONResponse({"error": "notification not found"}, status_code=404)


@router.post("/{notification_id}/ack")
async def api_acknowledge(
    notification_id: str,
    tenant_id: str = Depends(get_tenant_id),
):
    if acknowledge_notification(notification_id, tenant_id=tenant_id):
        return {"success": True, "id": notification_id}
    return JSONResponse({"error": "notification not found"}, status_code=404)


@router.get("")
async def api_list_notifications(
    fromAgent: str | None = Query(None),
    toAgent: str | None = Query(None),
    taskId: str | None = Query(None),
    limit: int = Query(50),
    tenant_id: str = Depends(get_tenant_id),
):
    return {
        "notifications": list_notifications(
            from_agent=fromAgent,
            to_agent=toAgent,
            task_id=taskId,
            limit=limit,
            tenant_id=tenant_id,
        )
    }
