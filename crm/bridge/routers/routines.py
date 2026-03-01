"""Routines routes — recurring task templates."""

from __future__ import annotations

from deps import get_tenant_id
from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from models import CreateRoutineRequest, UpdateRoutineRequest

from robothor.crm.dal import (
    advance_routine,
    create_routine,
    create_task,
    delete_routine,
    get_due_routines,
    list_routines,
    update_routine,
)
from robothor.events.bus import publish

router = APIRouter(prefix="/api", tags=["routines"])


@router.get("/routines")
async def api_list_routines(
    activeOnly: bool = Query(True),
    limit: int = Query(50),
    tenant_id: str = Depends(get_tenant_id),
):
    return {"routines": list_routines(active_only=activeOnly, limit=limit, tenant_id=tenant_id)}


@router.post("/routines")
async def api_create_routine(
    body: CreateRoutineRequest,
    x_agent_id: str | None = Header(None, alias="X-Agent-Id"),
    tenant_id: str = Depends(get_tenant_id),
):
    if not body.title:
        return JSONResponse({"error": "title required"}, status_code=400)

    # Validate cron expression
    try:
        from croniter import croniter

        croniter(body.cronExpr)
    except (ValueError, KeyError):
        return JSONResponse(
            {"error": f"Invalid cron expression: {body.cronExpr}"},
            status_code=400,
        )

    routine_id = create_routine(
        title=body.title,
        cron_expr=body.cronExpr,
        body=body.body,
        tz=body.timezone,
        assigned_to_agent=body.assignedToAgent,
        priority=body.priority,
        tags=body.tags,
        person_id=body.personId,
        company_id=body.companyId,
        created_by=x_agent_id or "helm-user",
        tenant_id=tenant_id,
    )
    if routine_id:
        publish(
            "agent",
            "routine.created",
            {
                "routine_id": routine_id,
                "title": body.title,
                "cron_expr": body.cronExpr,
                "tenant_id": tenant_id,
            },
            source="bridge",
        )
        return {"id": routine_id, "title": body.title}
    return JSONResponse({"error": "failed to create routine"}, status_code=500)


@router.patch("/routines/{routine_id}")
async def api_update_routine(
    routine_id: str,
    body: UpdateRoutineRequest,
    tenant_id: str = Depends(get_tenant_id),
):
    kwargs = {}
    field_map = {
        "title": "title",
        "body": "body",
        "cronExpr": "cron_expr",
        "timezone": "timezone",
        "assignedToAgent": "assigned_to_agent",
        "priority": "priority",
        "tags": "tags",
        "active": "active",
        "personId": "person_id",
        "companyId": "company_id",
    }
    for api_key, dal_key in field_map.items():
        val = getattr(body, api_key, None)
        if val is not None:
            kwargs[dal_key] = val

    # Validate cron if changing
    if body.cronExpr:
        try:
            from croniter import croniter

            croniter(body.cronExpr)
        except (ValueError, KeyError):
            return JSONResponse(
                {"error": f"Invalid cron expression: {body.cronExpr}"},
                status_code=400,
            )

    ok = update_routine(routine_id, tenant_id=tenant_id, **kwargs)
    if ok:
        return {"success": True, "id": routine_id}
    return JSONResponse({"error": "routine not found"}, status_code=404)


@router.delete("/routines/{routine_id}")
async def api_delete_routine(
    routine_id: str,
    tenant_id: str = Depends(get_tenant_id),
):
    if delete_routine(routine_id, tenant_id=tenant_id):
        return {"success": True, "id": routine_id}
    return JSONResponse({"error": "routine not found"}, status_code=404)


@router.post("/routines/trigger")
async def api_manual_trigger(
    tenant_id: str = Depends(get_tenant_id),
):
    """Manually trigger due routines (for testing)."""
    due = get_due_routines(tenant_id=tenant_id)
    triggered = []
    for routine in due:
        task_id = create_task(
            title=routine["title"],
            body=routine.get("body"),
            assigned_to_agent=routine.get("assignedToAgent"),
            priority=routine.get("priority", "normal"),
            tags=routine.get("tags"),
            person_id=routine.get("personId"),
            company_id=routine.get("companyId"),
            created_by_agent="routine-trigger",
            tenant_id=tenant_id,
        )
        if task_id:
            advance_routine(routine["id"])
            publish(
                "agent",
                "routine.triggered",
                {
                    "routine_id": routine["id"],
                    "task_id": task_id,
                    "title": routine["title"],
                    "tenant_id": tenant_id,
                },
                source="bridge",
            )
            triggered.append({"routineId": routine["id"], "taskId": task_id})
    return {"triggered": triggered, "count": len(triggered)}
