"""Tenant management routes."""

from __future__ import annotations

import logging
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
from robothor.crm.tenant_domains import InvalidSsoDomainError, SsoDomainConflictError
from robothor.sanitize import sanitize_log

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tenants", tags=["tenants"])

_OPERATOR_ROLES = frozenset({"owner", "admin"})
_ROOT_REQUIRED = JSONResponse(
    {"error": "root operator required to change the tenant hierarchy"}, status_code=403
)
_NOT_ACCESSIBLE = JSONResponse({"error": "tenant not accessible"}, status_code=403)


def _legacy_dev_mode() -> bool:
    import os

    from robothor.auth.runtime import legacy_headers_allowed

    return legacy_headers_allowed(bind_host=os.environ.get("ROBOTHOR_BRIDGE_HOST", "127.0.0.1"))


def _is_root_tenant_operator(request: Request) -> bool:
    """Whether the caller may shape the tenant hierarchy.

    Tenant switching trusts ``parent_tenant_id``, and ``crm_tenants`` has no
    RLS, so creating a tenant or changing its parent or ``active`` flag is
    reserved for a human owner/admin whose home AND acting tenant are both the
    platform tenant. A root admin acting in a child is not one: it must switch
    back first. Only the loopback insecure-dev mode, which has no identity at
    all, bypasses this. (A shared ``require_root_operator`` will replace it.)
    """
    # The same root require_operator gates on: ROBOTHOR_PLATFORM_TENANT, else
    # the default tenant. Read from the module at call time.
    from . import _operator

    auth = getattr(request.state, "auth", None)
    if auth is None:
        return _legacy_dev_mode()
    return (
        not auth.is_service
        and auth.role in _OPERATOR_ROLES
        and auth.tenant_id == _operator.PLATFORM_TENANT
        and auth.home_tenant_id == _operator.PLATFORM_TENANT
    )


def _may_edit_tenant(request: Request, tenant_id: str) -> bool:
    """Display name and settings: any operator, for a tenant it can reach."""
    from robothor.engine.permissions import resolve_accessible_tenants

    auth = getattr(request.state, "auth", None)
    if auth is None:
        return _legacy_dev_mode()
    if auth.is_service:
        return bool(tenant_id == auth.tenant_id)
    return tenant_id in resolve_accessible_tenants(auth.home_tenant_id, auth.role)


def _domain_error(exc: ValueError) -> JSONResponse:
    """409 for a domain another tenant claims, 422 for a malformed one.

    A fixed message, never the exception text: which tenant holds a domain
    is not this caller's to learn. The detail goes to the server log.
    """
    logger.info("tenant sso_domains refused: %s", sanitize_log(exc))
    if isinstance(exc, SsoDomainConflictError):
        return JSONResponse(
            {"error": "an sso domain is already claimed by another tenant"}, status_code=409
        )
    return JSONResponse({"error": "invalid sso_domains"}, status_code=422)


@router.get("")
def api_list_tenants(
    parentId: str | None = Query(None),
    activeOnly: bool = Query(True),
):
    return {"tenants": list_tenants(parent_id=parentId, active_only=activeOnly)}


@router.post("")
def api_create_tenant(body: CreateTenantRequest, request: Request):
    if not body.id or not body.displayName:
        return JSONResponse(
            {"error": "id and displayName required"},
            status_code=400,
        )
    if not _is_root_tenant_operator(request):
        return _ROOT_REQUIRED
    if body.parentTenantId:
        problem = crm_dal.tenant_parent_problem(body.id, body.parentTenantId)
        if problem:
            return JSONResponse({"error": problem}, status_code=422)
    try:
        tenant_id = create_tenant(
            tenant_id=body.id,
            display_name=body.displayName,
            parent_tenant_id=body.parentTenantId,
            settings=body.settings,
        )
    except (SsoDomainConflictError, InvalidSsoDomainError) as exc:
        return _domain_error(exc)
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
def api_update_tenant(tenant_id: str, body: UpdateTenantRequest, request: Request):
    # Compared with what is stored, so a form that sends every field back
    # unchanged is an edit, not a reshape: an unchanged parent or flag
    # changes nothing a child admin is not already allowed to keep.
    asks_to_reshape = body.parentTenantId is not None or body.active is not None
    stored = (crm_dal.get_tenant(tenant_id) or {}) if asks_to_reshape else {}
    new_parent = (
        body.parentTenantId
        if body.parentTenantId is not None and body.parentTenantId != stored.get("parentTenantId")
        else None
    )
    new_active = (
        body.active
        if body.active is not None and body.active != stored.get("active", True)
        else None
    )
    reshapes = new_parent is not None or new_active is not None
    if reshapes:
        if not _is_root_tenant_operator(request):
            return _ROOT_REQUIRED
        if new_parent is not None:
            problem = crm_dal.tenant_parent_problem(tenant_id, new_parent)
            if problem:
                return JSONResponse({"error": problem}, status_code=422)
    elif not _may_edit_tenant(request, tenant_id):
        return _NOT_ACCESSIBLE

    kwargs = {}
    if body.displayName is not None:
        kwargs["display_name"] = body.displayName
    if new_parent is not None:
        kwargs["parent_tenant_id"] = new_parent
    if body.settings is not None:
        kwargs["settings"] = body.settings
    if new_active is not None:
        kwargs["active"] = new_active

    try:
        updated = update_tenant(tenant_id, **kwargs)
    except (SsoDomainConflictError, InvalidSsoDomainError) as exc:
        return _domain_error(exc)
    if updated:
        return {"success": True, "id": tenant_id}
    return JSONResponse({"error": "tenant not found"}, status_code=404)
