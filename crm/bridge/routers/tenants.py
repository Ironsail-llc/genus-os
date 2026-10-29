"""Tenant management routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from models import CreateTenantRequest, UpdateTenantRequest

from robothor.crm import dal as crm_dal
from robothor.crm.dal import (
    create_tenant,
    get_tenant,
    list_tenants,
    update_tenant,
)

router = APIRouter(prefix="/api/tenants", tags=["tenants"])


@router.get("")
def api_list_tenants(
    parentId: str | None = Query(None),
    activeOnly: bool = Query(True),
):
    return {"tenants": list_tenants(parent_id=parentId, active_only=activeOnly)}


@router.post("")
def api_create_tenant(body: CreateTenantRequest):
    if not body.id or not body.displayName:
        return JSONResponse(
            {"error": "id and displayName required"},
            status_code=400,
        )
    tenant_id = create_tenant(
        tenant_id=body.id,
        display_name=body.displayName,
        parent_tenant_id=body.parentTenantId,
        settings=body.settings,
    )
    if tenant_id:
        return {"id": tenant_id, "displayName": body.displayName}
    return JSONResponse({"error": "failed to create tenant (may already exist)"}, status_code=409)


@router.get("/accessible", response_model=None)
def api_accessible_tenants(request: Request) -> list[dict[str, Any]] | JSONResponse:
    """The tenants this human may act in: home first, then its descendants.

    Rooted at the HOME tenant even while the caller is acting in a child, so
    the picker always offers the way back. Owners and admins get descendants
    to depth 3 (``resolve_accessible_tenants``); every other role gets only
    its home. Declared before ``/{tenant_id}`` so it is not read as an id.
    """
    from robothor.engine.permissions import resolve_accessible_tenants

    auth = getattr(request.state, "auth", None)
    if auth is None or auth.is_service:
        return JSONResponse({"error": "human user required"}, status_code=403)
    home = auth.home_tenant_id
    rows: list[dict[str, Any]] = []
    for tenant_id in resolve_accessible_tenants(home, auth.role):
        row = crm_dal.get_tenant(tenant_id) or {}
        rows.append(
            {
                "id": tenant_id,
                "display_name": row.get("displayName") or tenant_id,
                "parent_tenant_id": row.get("parentTenantId"),
                "is_home": tenant_id == home,
            }
        )
    rows.sort(key=lambda r: not r["is_home"])  # stable: home first, order kept
    return rows


@router.get("/{tenant_id}")
def api_get_tenant(tenant_id: str):
    result = get_tenant(tenant_id)
    if not result:
        return JSONResponse({"error": "tenant not found"}, status_code=404)
    return result


@router.patch("/{tenant_id}")
def api_update_tenant(tenant_id: str, body: UpdateTenantRequest):
    kwargs = {}
    if body.displayName is not None:
        kwargs["display_name"] = body.displayName
    if body.parentTenantId is not None:
        kwargs["parent_tenant_id"] = body.parentTenantId
    if body.settings is not None:
        kwargs["settings"] = body.settings
    if body.active is not None:
        kwargs["active"] = body.active

    if update_tenant(tenant_id, **kwargs):
        return {"success": True, "id": tenant_id}
    return JSONResponse({"error": "tenant not found"}, status_code=404)
