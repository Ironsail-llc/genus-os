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

``require_tenant_operator`` is the tenant-LOCAL sibling, for surfaces whose
rows each belong to one tenant (the Inbox: workflow approvals, agent questions,
proposed actions). It keeps conditions 1-3 and replaces 4 with "the session
names a tenant", and it hands that tenant back so the route reads and writes
only the acting tenant's rows. It is never sufficient for a platform-global
surface.
"""

from __future__ import annotations

import os
from typing import NamedTuple

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
#: Exactly three conditions of ``require_operator`` are kept: no service token
#: (an agent must not read the record of what was done to it), no other human
#: role, and the platform tenant only. Only the ROLE set widens, and only for
#: routes that are reads. Nothing that changes appliance state may use this —
#: ``test_mutations_are_gated.py`` walks the app for ``require_operator(`` and
#: would not see this name, which is the correct outcome rather than a gap:
#: this gate is not sufficient for a mutation and must never guard one.
AUDIT_ROLES = OPERATOR_ROLES | {"auditor"}


def require_operator(request: Request) -> str:
    auth = getattr(request.state, "auth", None)
    if (
        auth is None
        or auth.is_service
        or auth.role not in OPERATOR_ROLES
        or auth.tenant_id != PLATFORM_TENANT
    ):
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


def require_audit_reader(request: Request) -> str:
    """Gate for READ-ONLY audit surfaces: an operator, or an auditor.

    Returns the caller's role-qualified id, so a route that wants to record
    who read an export can name them.
    """
    auth = getattr(request.state, "auth", None)
    if (
        auth is None
        or auth.is_service
        or auth.role not in AUDIT_ROLES
        or auth.tenant_id != PLATFORM_TENANT
    ):
        raise HTTPException(status_code=403, detail="operator or auditor role required")
    return f"{auth.role}:{auth.actor_id}"
