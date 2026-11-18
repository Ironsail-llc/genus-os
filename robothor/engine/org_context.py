"""The ORGANISATION block: which tenant a run serves, and where it sits.

An agent that does not know whose assistant it is will guess. The operator-
facing agent, asked about its own organisation by the owner of a parent
tenant, knew neither the organisation's name nor that it had child tenants,
and read a misspelling of the organisation's own name as a CRM company to
search for. This block states the facts from ``crm_tenants``: this tenant,
its parent, the child tenants this run may reach, and one line on what the run
may do in them.

What a run may reach is ``AgentRun.accessible_tenant_ids``, resolved by
``run_identity.attach_tenant_access``: an owner or admin reaches descendants, a
member only its own tenant. A child this run cannot reach is not named.

Synchronous: it reads the database. Callers on the event loop run it through
``asyncio.to_thread``. It never raises; a failed lookup is logged and costs the
block, never the turn.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

ORGANISATION_HEADER = "--- ORGANISATION ---"


def _label(row: dict[str, Any]) -> str:
    return str(row.get("displayName") or row.get("id") or "")


def organisation_block(tenant_id: str, accessible_tenant_ids: Iterable[str] | None = None) -> str:
    """Render the ORGANISATION block for ``tenant_id``, or "" when it cannot."""
    try:
        return _render(tenant_id, tuple(accessible_tenant_ids or ()))
    except Exception as e:
        logger.warning("Organisation block for tenant %s failed: %s", tenant_id, e)
        return ""


def _render(tenant_id: str, accessible: tuple[str, ...]) -> str:
    from robothor.crm import dal

    tenant = dal.get_tenant(tenant_id)
    if not tenant:
        logger.warning("Organisation block: no crm_tenants row for tenant %s", tenant_id)
        return ""

    lines = [ORGANISATION_HEADER, f"You serve: {_label(tenant)} (tenant id: {tenant_id})."]

    parent_id = tenant.get("parentTenantId")
    if parent_id:
        parent = dal.get_tenant(parent_id) or {"id": parent_id}
        lines.append(f"Parent tenant: {_label(parent)} ({parent_id}).")
    else:
        lines.append("Parent tenant: none; this is a top-level tenant.")

    reach = set(accessible) - {tenant_id}
    children = dal.list_tenants(parent_id=tenant_id)
    reachable = [c for c in children if c.get("id") in reach]
    if not children:
        lines.append("Child tenants: none.")
    elif reachable:
        lines.append(f"Child tenants this run can reach ({len(reachable)}):")
        lines.extend(f"- {_label(c)} ({c['id']})" for c in reachable)
        deeper = len(reach - {c["id"] for c in reachable})
        if deeper:
            lines.append(f"Also reachable: {deeper} tenant(s) further down the hierarchy.")

    if reach:
        lines.append(
            "Access: this run may read and act in this tenant and in the child tenants "
            "listed above."
        )
    else:
        lines.append("Access: this run may read and act only in this tenant.")
    lines.append(
        "These names are this organisation's tenants, not a CRM company: when the user "
        "names one (or misspells it), they mean the tenant."
    )
    return "\n".join(lines)
