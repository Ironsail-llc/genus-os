"""The command centre: how each company under a parent tenant is doing.

``GET /api/org/overview`` answers a parent tenant's owner or admin, signed in
AT HOME, with one entry per tenant they may reach: their home tenant and its
descendants (``resolve_accessible_tenants``). The figures are
``robothor.tenancy.rollup.tenant_rollup``'s, the same function the
``org_overview`` engine tool answers with, so the page and the agent agree.

Refused (403) for a member or any other non-operator role, for a service
token, and for a human acting in another tenant: while acting, that tenant's
own pages are the ones to read.

Every other company read is audited, one row per company, filed under the
company read (as a tenant switch is filed under the tenant entered), so its
own admins can see who looked.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from routers._audit import audited
from routers._operator import OPERATOR_ROLES

router = APIRouter(prefix="/api/org", tags=["org"])


def _require_home_operator(request: Request) -> Any:
    auth = getattr(request.state, "auth", None)
    if auth is None or auth.is_service or auth.role not in OPERATOR_ROLES:
        raise HTTPException(status_code=403, detail="tenant owner or admin required")
    if auth.is_acting:
        raise HTTPException(
            status_code=403, detail="switch back to your home tenant to see every company"
        )
    return auth


@router.get("/overview")
def org_overview(request: Request) -> dict[str, Any]:
    """The rollup for the caller's home tenant and its descendants.

    A plain ``def``: FastAPI runs it in its threadpool, so the rollup's
    blocking reads stay off the event loop.
    """
    from robothor.auth.runtime import tenant_switch_mode
    from robothor.engine.permissions import resolve_accessible_tenants
    from robothor.tenancy import rollup

    auth = _require_home_operator(request)
    home = auth.home_tenant_id
    tenants = list(resolve_accessible_tenants(home, auth.role))
    result = rollup.tenant_rollup(tenants)
    for target in tenants:
        if target == home:
            continue
        audited(
            request,
            "org.overview",
            action=target,
            audit_tenant_id=target,
            user_id=auth.user_id,
            home_tenant_id=home,
            target_tenant_id=target,
        )
    return {
        "as_of": result["as_of"],
        "home_tenant_id": home,
        "has_children": len(tenants) > 1,
        "switching_enabled": tenant_switch_mode() == "enforce",
        "tenants": result["tenants"],
    }
