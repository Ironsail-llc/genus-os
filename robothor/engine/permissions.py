"""Hierarchical tenant access resolution.

Given a user's current tenant and role, determines which tenants they
can access.  Owner/admin roles get access to child tenants; regular
users only see their own tenant.

The hierarchy is read from ``crm_tenants.parent_tenant_id``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _get_child_tenants(tenant_id: str) -> list[str]:
    """Return direct child tenant IDs from the CRM database.

    Returns an empty list if the DB is unavailable or the tenant has
    no children — callers always degrade gracefully.
    """
    try:
        from robothor.crm.dal import list_tenants

        children: list[dict[str, Any]] = list_tenants(parent_id=tenant_id, active_only=True)
        return [t["id"] for t in children if "id" in t]
    except Exception:
        logger.debug("Could not fetch child tenants for %s", tenant_id, exc_info=True)
        return []


def resolve_accessible_tenants(
    tenant_id: str,
    user_role: str | None = None,
    *,
    max_depth: int = 3,
) -> tuple[str, ...]:
    """Return the tuple of tenant IDs a user may access.

    Rules:
        - Every user can access their own ``tenant_id``.
        - ``owner`` and ``admin`` roles additionally get all descendant
          tenants (children, grandchildren, ...) up to *max_depth* levels.
        - Other roles (``member``, ``viewer``, ``None``) only see their
          own tenant.

    Args:
        tenant_id: The user's home tenant.
        user_role: Role string (``"owner"``, ``"admin"``, ``"member"``,
            ``"viewer"``, or ``None``).
        max_depth: Maximum hierarchy depth to traverse (safety cap).

    Returns:
        A tuple of tenant ID strings, always containing at least
        ``tenant_id`` itself.
    """
    accessible = [tenant_id]

    if user_role not in ("owner", "admin"):
        return tuple(accessible)

    # BFS traversal of child tenants
    queue = [tenant_id]
    depth = 0
    try:
        while queue and depth < max_depth:
            next_level: list[str] = []
            for parent in queue:
                children = _get_child_tenants(parent)
                for child in children:
                    if child not in accessible:
                        accessible.append(child)
                        next_level.append(child)
            queue = next_level
            depth += 1
    except Exception:
        logger.debug("Tenant hierarchy traversal failed, using partial results", exc_info=True)

    return tuple(accessible)
