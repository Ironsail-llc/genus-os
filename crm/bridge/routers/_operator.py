"""The Helm's single authorization primitive.

Extracted from ``controls.py`` (Phase 1) so every operator-scoped Helm route —
Controls, Fleet, Runs, Workflows, Health — shares ONE gate. Four independent
conditions, none of which may be weakened:

1. ``auth is None``            → no verified session at all.
2. ``auth.is_service``         → agent/service tokens, structurally barred.
3. ``auth.role not in OPERATOR_ROLES`` → dashboard SSO admits any verified org
   member; only owner/admin are operators.
4. ``auth.tenant_id != PLATFORM_TENANT`` → the flags/health/fleet surfaces are
   platform-global; an operator of another tenant must not see or touch them.

Tenant switching split that one gate in two. ``require_tenant_admin`` keeps
conditions 1-3 and lets the ACTING tenant (``auth.tenant_id``) be any tenant:
it guards a tenant's own administration — its fleet, runs, approvals, accounts
and provider keys — and every query behind it is scoped by that tenant.
``require_root_operator`` keeps all four and adds a fifth: the token must be AT
HOME (``home_tenant_id == tenant_id``), because a root admin acting in a child
is that child's administrator for the request, not the appliance's.
``require_operator`` is the root gate under its old name.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import HTTPException, Request

from robothor.constants import DEFAULT_TENANT

# ROBOTHOR_PLATFORM_TENANT overrides; otherwise the gate agrees with the rest
# of the bridge on DEFAULT_TENANT (which itself honors ROBOTHOR_DEFAULT_TENANT).
# Never a hardcoded instance tenant id — see CLAUDE.md rule 1.
PLATFORM_TENANT = os.environ.get("ROBOTHOR_PLATFORM_TENANT") or DEFAULT_TENANT

OPERATOR_ROLES = frozenset({"owner", "admin"})

#: Who may READ an audit trail. The operator roles plus ``auditor``, which
#: ``robothor.auth.tokens`` describes as "Read-only, plus the audit log. For
#: review, not operation." and grants ``audit:read`` and nothing that writes.
#:
#: The audit gates keep every other condition of their operator counterparts:
#: no service token (an agent must not read the record of what was done to
#: it), no other human role. Only the ROLE set widens, and only for routes that
#: are reads. Nothing that changes state may use them —
#: ``test_mutations_are_gated.py`` does not count them as gates, which is the
#: correct outcome rather than a gap.
AUDIT_ROLES = OPERATOR_ROLES | {"auditor"}


def _auth(request: Request) -> Any:
    return getattr(request.state, "auth", None)


def _human_with_role(auth: Any, roles: frozenset[str]) -> bool:
    return auth is not None and not auth.is_service and auth.role in roles


def is_root(auth: Any) -> bool:
    """Whether this identity is AT HOME in the platform tenant.

    Both halves: a root admin acting in a child tenant carries
    ``tenant_id == <child>`` and ``home_tenant_id == PLATFORM_TENANT``, and is
    not root for that request — what it does there, it does as the child's
    administrator. Read at call time so the module global can be overridden.
    """
    return (
        auth is not None
        and auth.tenant_id == PLATFORM_TENANT
        and getattr(auth, "home_tenant_id", auth.tenant_id) == PLATFORM_TENANT
    )


def require_tenant_admin(request: Request) -> str:
    """Gate for the ACTING tenant's own administration.

    A human owner or admin whose token acts in the tenant — the tenant's own
    admin, or a root admin who switched in. No hierarchy lookup here: whether
    the caller may act in ``auth.tenant_id`` at all was decided when the
    acting token was minted (``POST /api/auth/switch-tenant``), and every
    query behind this gate is scoped by that same verified tenant.
    """
    auth = _auth(request)
    if not _human_with_role(auth, OPERATOR_ROLES):
        raise HTTPException(status_code=403, detail="tenant administrator role required")
    return f"operator:{auth.actor_id}"


def require_tenant_audit_reader(request: Request) -> str:
    """Gate for READ-ONLY audit surfaces of the acting tenant: admin or auditor.

    Never for a mutation — see :data:`AUDIT_ROLES`.
    """
    auth = _auth(request)
    if not _human_with_role(auth, AUDIT_ROLES):
        raise HTTPException(status_code=403, detail="operator or auditor role required")
    return f"{auth.role}:{auth.actor_id}"


def require_root_operator(request: Request) -> str:
    """Gate for appliance-wide state: a root owner/admin, at home.

    Guardrail flags, settings, logs, plugins and the doctor are not any one
    tenant's, so the four original conditions all hold, plus the fifth that
    tenant switching made necessary: the token must not be acting.
    """
    auth = _auth(request)
    if not _human_with_role(auth, OPERATOR_ROLES) or not is_root(auth):
        raise HTTPException(status_code=403, detail="operator role required")
    return f"operator:{auth.actor_id}"


def require_root_audit_reader(request: Request) -> str:
    """Gate for READ-ONLY appliance-wide trails: a root operator or auditor, at home.

    Returns the caller's role-qualified id, so a route that wants to record
    who read an export can name them.
    """
    auth = _auth(request)
    if not _human_with_role(auth, AUDIT_ROLES) or not is_root(auth):
        raise HTTPException(status_code=403, detail="operator or auditor role required")
    return f"{auth.role}:{auth.actor_id}"


#: The pre-tenant-switching names, kept as the ROOT gates so every route and
#: test written against them keeps its meaning.
require_operator = require_root_operator
require_audit_reader = require_root_audit_reader


def audit_visibility(request: Request) -> tuple[tuple[str, ...], bool]:
    """``(tenants, include_untenanted)`` an audit read by this caller may see.

    The acting tenant and, for an owner/admin, its descendants
    (``resolve_accessible_tenants``). Rows written before ``audit_log`` had a
    tenant are NULL — appliance-wide — and only a root reader at home sees
    them. Rooted at the ACTING tenant, not the home one: a root admin who
    switched into a child is reading that child's trail.
    """
    from robothor.engine.permissions import resolve_accessible_tenants

    auth = _auth(request)
    tenants = tuple(resolve_accessible_tenants(str(auth.tenant_id), auth.role))
    return tenants, is_root(auth)
