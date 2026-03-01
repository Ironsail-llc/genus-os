"""Notes & Tasks routes."""

from __future__ import annotations

import re
from datetime import UTC

from deps import get_tenant_id
from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from models import (
    ApproveTaskRequest,
    CreateNoteRequest,
    CreateTaskRequest,
    RejectTaskRequest,
    UpdateTaskRequest,
)

from robothor.crm.dal import (
    approve_task,
    create_note,
    create_task,
    delete_note,
    delete_task,
    find_task_by_thread_id,
    get_note,
    get_task,
    get_task_history,
    list_agent_tasks,
    list_notes,
    list_tasks,
    reject_task,
    resolve_task,
    send_notification,
    update_task,
)
from robothor.events.bus import publish

router = APIRouter(prefix="/api", tags=["notes", "tasks"])


# ─── Notes ───────────────────────────────────────────────────────────────


@router.get("/notes")
async def api_list_notes(
    personId: str | None = Query(None),
    companyId: str | None = Query(None),
    limit: int = Query(50),
    tenant_id: str = Depends(get_tenant_id),
):
    return {"notes": list_notes(personId, companyId, limit, tenant_id=tenant_id)}


@router.post("/notes")
async def api_create_note(
    body: CreateNoteRequest,
    tenant_id: str = Depends(get_tenant_id),
):
    if not body.title:
        return JSONResponse({"error": "title required"}, status_code=400)
    note_id = create_note(body.title, body.body, body.personId, body.companyId, tenant_id=tenant_id)
    if note_id:
        return {"id": note_id, "title": body.title}
    return JSONResponse({"error": "failed to create note"}, status_code=500)


@router.get("/notes/{note_id}")
async def api_get_note(
    note_id: str,
    tenant_id: str = Depends(get_tenant_id),
):
    result = get_note(note_id, tenant_id=tenant_id)
    if not result:
        return JSONResponse({"error": "note not found"}, status_code=404)
    return result


@router.delete("/notes/{note_id}")
async def api_delete_note(
    note_id: str,
    tenant_id: str = Depends(get_tenant_id),
):
    if delete_note(note_id, tenant_id=tenant_id):
        return {"success": True, "id": note_id}
    return JSONResponse({"error": "note not found"}, status_code=404)


# ─── Tasks ───────────────────────────────────────────────────────────────


@router.get("/tasks")
async def api_list_tasks(
    status: str | None = Query(None),
    personId: str | None = Query(None),
    assignedToAgent: str | None = Query(None),
    createdByAgent: str | None = Query(None),
    tags: str | None = Query(None),
    priority: str | None = Query(None),
    excludeResolved: bool = Query(False),
    limit: int = Query(50),
    tenant_id: str = Depends(get_tenant_id),
):
    tag_list = [t.strip() for t in tags.split(",")] if tags else None
    return {
        "tasks": list_tasks(
            status=status,
            person_id=personId,
            limit=limit,
            assigned_to_agent=assignedToAgent,
            created_by_agent=createdByAgent,
            tags=tag_list,
            priority=priority,
            exclude_resolved=excludeResolved,
            tenant_id=tenant_id,
        )
    }


@router.post("/tasks")
async def api_create_task(
    body: CreateTaskRequest,
    x_agent_id: str | None = Header(None, alias="X-Agent-Id"),
    tenant_id: str = Depends(get_tenant_id),
):
    if not body.title:
        return JSONResponse({"error": "title required"}, status_code=400)
    # Server-side dedup: check for existing task with same threadId
    if body.body and body.assignedToAgent:
        m = re.search(r"threadId:\s*([a-zA-Z0-9]+)", body.body)
        if m:
            existing = find_task_by_thread_id(
                m.group(1), assigned_to_agent=body.assignedToAgent, tenant_id=tenant_id
            )
            if existing:
                return {"id": existing["id"], "title": existing["title"], "deduplicated": True}
    # Auto-populate created_by_agent from X-Agent-Id header
    agent_id = x_agent_id
    task_id = create_task(
        title=body.title,
        body=body.body,
        status=body.status,
        due_at=body.dueAt,
        person_id=body.personId,
        company_id=body.companyId,
        created_by_agent=agent_id,
        assigned_to_agent=body.assignedToAgent,
        priority=body.priority,
        tags=body.tags,
        parent_task_id=body.parentTaskId,
        tenant_id=tenant_id,
    )
    if task_id:
        publish(
            "agent",
            "task.created",
            {
                "task_id": task_id,
                "title": body.title,
                "assigned_to_agent": body.assignedToAgent,
                "created_by_agent": agent_id,
                "priority": body.priority,
                "tags": body.tags or [],
                "tenant_id": tenant_id,
            },
            source="bridge",
        )
        # Auto-send task_assigned notification
        if body.assignedToAgent and agent_id:
            send_notification(
                from_agent=agent_id,
                to_agent=body.assignedToAgent,
                notification_type="task_assigned",
                subject=f"New task: {body.title}",
                body=body.body,
                task_id=task_id,
                tenant_id=tenant_id,
            )
        return {"id": task_id, "title": body.title}
    return JSONResponse({"error": "failed to create task"}, status_code=500)


@router.get("/tasks/agent/{agent_id}")
async def api_list_agent_tasks(
    agent_id: str,
    status: str | None = Query(None),
    includeUnassigned: bool = Query(False),
    limit: int = Query(50),
    tenant_id: str = Depends(get_tenant_id),
):
    return {
        "tasks": list_agent_tasks(
            agent_id=agent_id,
            include_unassigned=includeUnassigned,
            status=status,
            limit=limit,
            tenant_id=tenant_id,
        )
    }


@router.get("/tasks/{task_id}")
async def api_get_task(
    task_id: str,
    tenant_id: str = Depends(get_tenant_id),
):
    result = get_task(task_id, tenant_id=tenant_id)
    if not result:
        return JSONResponse({"error": "task not found"}, status_code=404)
    return result


@router.patch("/tasks/{task_id}")
async def api_update_task(
    task_id: str,
    body: UpdateTaskRequest,
    x_agent_id: str | None = Header(None, alias="X-Agent-Id"),
    tenant_id: str = Depends(get_tenant_id),
):
    kwargs = {}
    field_map = {
        "title": "title",
        "body": "body",
        "status": "status",
        "dueAt": "due_at",
        "personId": "person_id",
        "companyId": "company_id",
        "assignedToAgent": "assigned_to_agent",
        "priority": "priority",
        "tags": "tags",
        "parentTaskId": "parent_task_id",
        "resolution": "resolution",
    }
    for api_key, dal_key in field_map.items():
        val = getattr(body, api_key, None)
        if val is not None:
            kwargs[dal_key] = val
    # Auto-set resolved_at when status changes to DONE
    if body.status and body.status.upper() == "DONE":
        from datetime import datetime

        kwargs["resolved_at"] = datetime.now(UTC).isoformat()
    result = update_task(task_id, changed_by=x_agent_id, tenant_id=tenant_id, **kwargs)
    # Handle transition validation errors
    if isinstance(result, dict) and "error" in result:
        return JSONResponse(result, status_code=422)
    if result:
        response = {"success": True, "id": task_id}
        if isinstance(result, dict) and "warning" in result:
            response["warning"] = result["warning"]
        publish(
            "agent",
            "task.updated",
            {
                "task_id": task_id,
                "fields": list(kwargs.keys()),
                "agent_id": x_agent_id,
                "tenant_id": tenant_id,
            },
            source="bridge",
        )
        # Auto-send review_requested notification when moving to REVIEW
        if body.status and body.status.upper() == "REVIEW" and x_agent_id:
            send_notification(
                from_agent=x_agent_id,
                to_agent="main",
                notification_type="review_requested",
                subject=f"Review requested: {task_id}",
                task_id=task_id,
                tenant_id=tenant_id,
            )
        return response
    return JSONResponse({"error": "task not found"}, status_code=404)


@router.post("/tasks/{task_id}/resolve")
async def api_resolve_task(
    task_id: str,
    body: dict,
    x_agent_id: str | None = Header(None, alias="X-Agent-Id"),
    tenant_id: str = Depends(get_tenant_id),
):
    resolution = body.get("resolution", "")
    if not resolution:
        return JSONResponse({"error": "resolution required"}, status_code=400)
    ok = resolve_task(task_id, resolution, agent_id=x_agent_id, tenant_id=tenant_id)
    if ok:
        publish(
            "agent",
            "task.resolved",
            {
                "task_id": task_id,
                "resolution": resolution,
                "agent_id": x_agent_id,
                "tenant_id": tenant_id,
            },
            source="bridge",
        )
        return {"success": True, "id": task_id}
    return JSONResponse({"error": "task not found"}, status_code=404)


@router.post("/tasks/{task_id}/approve")
async def api_approve_task(
    task_id: str,
    body: ApproveTaskRequest,
    x_agent_id: str | None = Header(None, alias="X-Agent-Id"),
    tenant_id: str = Depends(get_tenant_id),
):
    reviewer = x_agent_id or "helm-user"
    result = approve_task(task_id, body.resolution, reviewer, tenant_id=tenant_id)
    if isinstance(result, dict) and "error" in result:
        return JSONResponse(result, status_code=422)
    if result:
        publish(
            "agent",
            "task.approved",
            {
                "task_id": task_id,
                "reviewer": reviewer,
                "tenant_id": tenant_id,
            },
            source="bridge",
        )
        return {"success": True, "id": task_id}
    return JSONResponse({"error": "task not found"}, status_code=404)


@router.post("/tasks/{task_id}/reject")
async def api_reject_task(
    task_id: str,
    body: RejectTaskRequest,
    x_agent_id: str | None = Header(None, alias="X-Agent-Id"),
    tenant_id: str = Depends(get_tenant_id),
):
    reviewer = x_agent_id or "helm-user"
    result = reject_task(
        task_id,
        body.reason,
        reviewer,
        change_requests=body.changeRequests,
        tenant_id=tenant_id,
    )
    if isinstance(result, dict) and "error" in result:
        return JSONResponse(result, status_code=422)
    if result:
        publish(
            "agent",
            "task.rejected",
            {
                "task_id": task_id,
                "reviewer": reviewer,
                "tenant_id": tenant_id,
            },
            source="bridge",
        )
        return {"success": True, "id": task_id}
    return JSONResponse({"error": "task not found"}, status_code=404)


@router.get("/tasks/{task_id}/history")
async def api_get_task_history(
    task_id: str,
    limit: int = Query(50),
    tenant_id: str = Depends(get_tenant_id),
):
    history = get_task_history(task_id, limit=limit, tenant_id=tenant_id)
    return {"history": history, "count": len(history)}


@router.delete("/tasks/{task_id}")
async def api_delete_task(
    task_id: str,
    tenant_id: str = Depends(get_tenant_id),
):
    if delete_task(task_id, tenant_id=tenant_id):
        return {"success": True, "id": task_id}
    return JSONResponse({"error": "task not found"}, status_code=404)
