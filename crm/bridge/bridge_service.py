"""
Robothor Bridge Service — Connects OpenClaw, CRM, and Memory System.

FastAPI app on port 9100. All CRM operations go through robothor.crm.dal.
Agent RBAC enforced via X-Agent-Id header middleware.

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

import httpx
from fastapi import FastAPI

from middleware import CorrelationMiddleware, RBACMiddleware
from routers.conversations import router as conversations_router
from routers.health import router as health_router
from routers.integration import router as integration_router
from routers.memory import router as memory_router
from routers.notes_tasks import router as notes_tasks_router
from routers.people import router as people_router

# ─── Configuration ───────────────────────────────────────────────────────

_bridge_config: dict = {
    "memory_url": os.getenv("MEMORY_URL", "http://localhost:9099"),
    "impetus_one_url": os.getenv("IMPETUS_ONE_BASE_URL", "http://localhost:8000"),
    "impetus_one_token": os.getenv("IMPETUS_ONE_API_TOKEN", ""),
}

http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(timeout=30.0)
    yield
    await http_client.aclose()


# ─── App Assembly ────────────────────────────────────────────────────────

app = FastAPI(
    title="Robothor Bridge",
    version="2.0.0",
    description="Bridge between OpenClaw agents and the Robothor intelligence layer.",
    lifespan=lifespan,
)

# Middleware (applied in reverse order — correlation runs first)
app.add_middleware(RBACMiddleware)
app.add_middleware(CorrelationMiddleware)

# Routers
app.include_router(health_router)
app.include_router(people_router)
app.include_router(conversations_router)
app.include_router(notes_tasks_router)
app.include_router(memory_router)
app.include_router(integration_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9100)
