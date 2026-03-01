"""Tenant management routes."""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from models import CreateTenantRequest, UpdateTenantRequest

from robothor.crm.dal import (
    create_tenant,
    get_tenant,
    list_tenants,
    update_tenant,
)

router = APIRouter(prefix="/api/tenants", tags=["tenants"])


@router.get("")
async def api_list_tenants(
    parentId: str | None = Query(None),
    activeOnly: bool = Query(True),
):
    return {"tenants": list_tenants(parent_id=parentId, active_only=activeOnly)}


@router.post("")
async def api_create_tenant(body: CreateTenantRequest):
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


@router.get("/{tenant_id}")
async def api_get_tenant(tenant_id: str):
    result = get_tenant(tenant_id)
    if not result:
        return JSONResponse({"error": "tenant not found"}, status_code=404)
    return result


@router.patch("/{tenant_id}")
async def api_update_tenant(tenant_id: str, body: UpdateTenantRequest):
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
