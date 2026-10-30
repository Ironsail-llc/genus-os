"""Agent-to-Agent Notification routes.

The inbox read is **caller-scoped**, and that is newer than the rest of this
router. ``get_agent_inbox`` matches ``to_agent`` and ``tenant_id`` and nothing
else, which was survivable while ``to_agent`` held AGENT ids and the bodies were
ops chatter. The webchat channel
(:mod:`robothor.engine.channels.webchat`) writes a member's own delivery here
with ``to_agent = <user_accounts.id>``, so an un-scoped read would let any
authenticated same-tenant member read another member's chat deliveries —
bypassing, through this half, exactly the isolation the per-user session work
provides on the other. See :func:`_refuse_foreign_inbox`.
"""

from __future__ import annotations

from deps import get_tenant_id
from fastapi import APIRouter, Depends, Query, Request
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
from routers._operator import OPERATOR_ROLES

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.post("/send")
def api_send_notification(
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


def _refuse_foreign_inbox(request: Request, agent_id: str) -> JSONResponse | None:
    """``None`` when this caller may read ``agent_id``'s inbox, else a 403.

    Owner and admin may read any of them — the same two roles ``require_operator``
    admits, and an operator triaging the fleet needs it. Everybody else may read
    only the inbox whose id is their own ``actor_id``: the ``user_accounts.id``
    for a human session (which is what the webchat channel addresses a delivery
    to) and the agent id for a service token.

    A missing ``auth`` is not a hole here: ``AuthMiddleware`` answers 401 before
    any route is reached whenever authentication is enforced, so ``None`` means
    the bridge is in loopback-only insecure development mode, where no route is
    gated at all. Deciding otherwise would 403 every request in that mode
    including an operator's.
    """
    auth = getattr(request.state, "auth", None)
    if auth is None or auth.role in OPERATOR_ROLES:
        return None
    if agent_id == auth.actor_id:
        return None
    # No id in the message: this is returned to the caller who asked for
    # somebody else's inbox, and naming the account confirms it exists.
    return JSONResponse({"error": "that inbox belongs to somebody else"}, status_code=403)


@router.get("/inbox/{agent_id}")
def api_get_inbox(
    agent_id: str,
    request: Request,
    unreadOnly: bool = Query(True),
    typeFilter: str | None = Query(None),
    limit: int = Query(50),
    tenant_id: str = Depends(get_tenant_id),
):
    refusal = _refuse_foreign_inbox(request, agent_id)
    if refusal is not None:
        return refusal
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
def api_mark_read(
    notification_id: str,
    tenant_id: str = Depends(get_tenant_id),
):
    if mark_notification_read(notification_id, tenant_id=tenant_id):
        return {"success": True, "id": notification_id}
    return JSONResponse({"error": "notification not found"}, status_code=404)


@router.post("/{notification_id}/ack")
def api_acknowledge(
    notification_id: str,
    tenant_id: str = Depends(get_tenant_id),
):
    if acknowledge_notification(notification_id, tenant_id=tenant_id):
        return {"success": True, "id": notification_id}
    return JSONResponse({"error": "notification not found"}, status_code=404)


@router.get("")
def api_list_notifications(
    request: Request,
    fromAgent: str | None = Query(None),
    toAgent: str | None = Query(None),
    taskId: str | None = Query(None),
    limit: int = Query(50),
    tenant_id: str = Depends(get_tenant_id),
):
    """List notifications. Non-operators are scoped to their own.

    Gating the ``/inbox/{id}`` path alone would have been theatre: this route
    reads the same rows with ``to_agent`` as a QUERY parameter, and with no
    filter at all it answers with the whole tenant's notifications — every
    member's webchat delivery included. So the same rule applies here, in the
    only two shapes it can take: a non-operator asking for somebody else's is
    refused, and one asking for nothing in particular gets their OWN rather than
    everyone's. Guard the path every caller crosses, not the one the current
    caller uses.
    """
    auth = getattr(request.state, "auth", None)
    recipient = toAgent
    if auth is not None and auth.role not in OPERATOR_ROLES:
        if toAgent and toAgent != auth.actor_id:
            return JSONResponse(
                {"error": "those notifications belong to somebody else"}, status_code=403
            )
        recipient = auth.actor_id
    return {
        "notifications": list_notifications(
            from_agent=fromAgent,
            to_agent=recipient,
            task_id=taskId,
            limit=limit,
            tenant_id=tenant_id,
        )
    }
