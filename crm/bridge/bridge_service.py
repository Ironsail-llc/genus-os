"""
Robothor Bridge Service — Connects agents, CRM, and Memory System.

FastAPI app on port 9100. All CRM operations go through robothor.crm.dal.
Agent RBAC enforced via X-Agent-Id header middleware.
Tenant isolation via X-Tenant-Id header middleware.

OpenAPI docs: http://localhost:9100/docs
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager

# Prevent double-import when run as __main__: ensure 'bridge_service' module
# name resolves to THIS instance so routers see the same http_client.
if __name__ == "__main__":
    sys.modules["bridge_service"] = sys.modules[__name__]

import asyncio
import logging

import httpx
from fastapi import FastAPI

logger = logging.getLogger(__name__)

from middleware import CorrelationMiddleware, RBACMiddleware, TenantMiddleware
from routers.agents import router as agents_router
from routers.conversations import router as conversations_router
from routers.health import router as health_router
from routers.integration import router as integration_router
from routers.memory import router as memory_router
from routers.notes_tasks import router as notes_tasks_router
from routers.notifications import router as notifications_router
from routers.people import router as people_router
from routers.routines import router as routines_router
from routers.tenants import router as tenants_router

# ─── Configuration ───────────────────────────────────────────────────────

_bridge_config: dict = {
    "memory_url": os.getenv("MEMORY_URL", "http://localhost:9099"),
    "impetus_one_url": os.getenv("IMPETUS_ONE_BASE_URL", "http://localhost:8000"),
    "impetus_one_token": os.getenv("IMPETUS_ONE_API_TOKEN", ""),
}

http_client: httpx.AsyncClient | None = None


async def _routine_trigger_loop():
    """Background task: check for due routines every 60s and create tasks."""
    from robothor.crm.dal import advance_routine, create_task, get_due_routines
    from robothor.events.bus import publish

    while True:
        try:
            await asyncio.sleep(60)
            due = get_due_routines()
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
                    tenant_id=routine.get("tenantId", "robothor-primary"),
                )
                if task_id:
                    advance_routine(routine["id"])
                    publish("agent", "routine.triggered", {
                        "routine_id": routine["id"], "task_id": task_id,
                        "title": routine["title"],
                    }, source="bridge")
                    logger.info("Routine '%s' triggered → task %s", routine["title"], task_id)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Routine trigger loop error: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(timeout=30.0)
    trigger_task = asyncio.create_task(_routine_trigger_loop())
    yield
    trigger_task.cancel()
    try:
        await trigger_task
    except asyncio.CancelledError:
        pass
    await http_client.aclose()


# ─── App Assembly ────────────────────────────────────────────────────────

app = FastAPI(
    title="Robothor Bridge",
    version="3.0.0",
    description="Bridge between agents and the Robothor intelligence layer. Multi-tenant.",
    lifespan=lifespan,
)

# Middleware (applied in reverse order — correlation runs first, then tenant, then RBAC)
app.add_middleware(RBACMiddleware)
app.add_middleware(TenantMiddleware)
app.add_middleware(CorrelationMiddleware)

# Routers
app.include_router(health_router)
app.include_router(agents_router)
app.include_router(people_router)
app.include_router(conversations_router)
app.include_router(notes_tasks_router)
app.include_router(memory_router)
app.include_router(routines_router)
app.include_router(notifications_router)
app.include_router(tenants_router)
app.include_router(integration_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9100)
