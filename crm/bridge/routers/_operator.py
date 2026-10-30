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

``require_tenant_operator`` is the tenant-LOCAL sibling, for surfaces whose
rows each belong to one tenant (the Inbox: workflow approvals, agent questions,
proposed actions). It keeps conditions 1-3 and replaces 4 with "the session
names a tenant", and it hands that tenant back so the route reads and writes
only the acting tenant's rows. It is never sufficient for a platform-global
surface.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from fastapi import HTTPException, Request

# ROBOTHOR_PLATFORM_TENANT overrides; otherwise the gate agrees with the rest
# of the bridge on DEFAULT_TENANT (which itself honors ROBOTHOR_DEFAULT_TENANT).
# Never a hardcoded instance tenant id — see CLAUDE.md rule 1.
from robothor.constants import PLATFORM_TENANT as PLATFORM_TENANT

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


class TenantOperator(NamedTuple):
    """Who may decide, and the one tenant whose rows they may decide."""

    actor: str
    tenant_id: str


def require_tenant_operator(request: Request) -> TenantOperator:
    """Gate for TENANT-LOCAL decisions: an owner or admin of the acting tenant.

    The tenant comes from the verified session (``auth.tenant_id``), never from
    the request, so a caller cannot name someone else's tenant. With tenant
    switching, that is the tenant the token acts in. The platform tenant's
    operator passes too, with the platform tenant, so their Inbox is unchanged.
    """
    auth = getattr(request.state, "auth", None)
    tenant_id = (getattr(auth, "tenant_id", None) or "").strip()
    if auth is None or auth.is_service or auth.role not in OPERATOR_ROLES or not tenant_id:
        raise HTTPException(status_code=403, detail="operator role required")
    return TenantOperator(actor=f"operator:{auth.actor_id}", tenant_id=tenant_id)


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


def audit_scope(auth: Any) -> list[str] | None:
    """The tenants an audit read by *auth* may see; ``None`` means unrestricted.

    One answer for every audit read site:

    * the platform root operator or auditor AT HOME reads the appliance-wide
      trail — every tenant, the rows written before ``audit_log`` recorded a
      tenant (NULL), and independent roots (such as the benchmark sandbox)
      that no hierarchy walk from the platform tenant reaches — exactly as
      before tenants were recorded;
    * anyone else, a root admin acting in a child included, reads the ACTING
      tenant and, for an owner/admin, its descendants
      (``resolve_accessible_tenants``), and never the NULL rows.
    """
    if is_root(auth):
        return None
    from robothor.engine.permissions import resolve_accessible_tenants

    return list(resolve_accessible_tenants(str(auth.tenant_id), auth.role))
