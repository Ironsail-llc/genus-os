"""Tenant operator goal controls; all mutations use the shared lifecycle service."""

from __future__ import annotations

from typing import Literal
from uuid import UUID  # noqa: TC003 — FastAPI resolves annotations at runtime

from deps import get_tenant_id
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from robothor.goals import store
from robothor.goals.model import CreateGoal, GoalUpdate  # noqa: TC001

router = APIRouter(prefix="/api/goals", tags=["goals"])


def goal_operator(request: Request) -> str:
    auth = getattr(request.state, "auth", None)
    if not auth or auth.is_service or auth.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="tenant operator role required")
    return str(auth.actor_id)


def call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ValueError as exc:
        message = str(exc)
        code = 409 if "stale" in message else 404 if "not found" in message else 422
        raise HTTPException(status_code=code, detail=message) from exc


class EnabledRequest(BaseModel):
    enabled: bool


class AdoptRequest(BaseModel):
    legacy_task_id: UUID


@router.get("")
def list_goals(tenant: str = Depends(get_tenant_id), actor: str = Depends(goal_operator)):
    return {"goals": store.list_goals(tenant), "enabled": store.enabled(tenant)}


@router.post("")
def create_goal(
    body: CreateGoal, tenant: str = Depends(get_tenant_id), actor: str = Depends(goal_operator)
):
    return {"goal": call(store.create, tenant, body, actor)}


@router.patch("/settings")
def configure(
    body: EnabledRequest, tenant: str = Depends(get_tenant_id), actor: str = Depends(goal_operator)
):
    store.set_enabled(tenant, body.enabled, actor)
    return {"enabled": body.enabled}


@router.post("/adopt")
def adopt(
    body: AdoptRequest, tenant: str = Depends(get_tenant_id), actor: str = Depends(goal_operator)
):
    return {"goal": call(store.adopt, tenant, str(body.legacy_task_id), actor)}


@router.get("/{goal_id}")
def get_goal(
    goal_id: UUID, tenant: str = Depends(get_tenant_id), actor: str = Depends(goal_operator)
):
    return {"goal": call(store.get, tenant, str(goal_id))}


@router.patch("/{goal_id}")
def update_goal(
    goal_id: UUID,
    body: GoalUpdate,
    tenant: str = Depends(get_tenant_id),
    actor: str = Depends(goal_operator),
):
    return {"goal": call(store.update, tenant, str(goal_id), body, actor, operator=True)}


class RuntimeControl(BaseModel):
    action: Literal["pause", "cancel"]
    note: str = ""


@router.post("/runs/{run_id}/control")
def control_runtime(
    run_id: UUID,
    body: RuntimeControl,
    tenant: str = Depends(get_tenant_id),
    actor: str = Depends(goal_operator),
):
    from robothor.engine.runtime.controls import issue

    return call(issue, tenant, str(run_id), body.action, body.note)
