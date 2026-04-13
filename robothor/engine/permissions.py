"""Per-user permission enforcement for Genus OS.

Checks whether a user's role permits a given tool call, and resolves
which tenants a user can access based on the tenant hierarchy.

Enforcement is opt-in: if ``user_role`` is empty (cron, hooks, system
triggers), all tools are allowed.  This preserves backward compatibility
for single-tenant instances and automated agent runs.

Permission rules live in the ``role_permissions`` database table, with
a ``__default__`` tenant providing platform-wide defaults that any
tenant can override.

Evaluation order (first match wins):
    1. Tenant-specific DENY  →  blocked
    2. Tenant-specific ALLOW →  allowed
    3. ``__default__`` DENY  →  blocked
    4. ``__default__`` ALLOW →  allowed
    5. No match              →  denied (fail-closed for unconfigured roles)

Hierarchical tenant access resolution:
    Given a user's current tenant and role, determines which tenants they
    can access.  Owner/admin roles get access to child tenants; regular
    users only see their own tenant.

    The hierarchy is read from ``crm_tenants.parent_tenant_id``.
"""

from __future__ import annotations

import fnmatch
import logging
from typing import Any

from robothor.constants import DEFAULT_TENANT

logger = logging.getLogger(__name__)


def check_tool_permission(
    user_role: str,
    tenant_id: str,
    tool_name: str,
) -> str | None:
    """Check if a user role is allowed to execute a tool.

    Args:
        user_role: The user's role (viewer, user, admin, owner).
            Empty string means system/automated — always allowed.
        tenant_id: The tenant to check permissions for.
        tool_name: The tool being invoked.

    Returns:
        Denial reason string, or None if allowed.
    """
    if not user_role:
        return None  # System/automated — no user-level enforcement

    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()

            # Fetch matching rules: tenant-specific first, then __default__
            cur.execute(
                """
                SELECT tool_pattern, access, tenant_id
                FROM role_permissions
                WHERE role = %s AND tenant_id IN (%s, '__default__')
                ORDER BY
                    CASE WHEN tenant_id = %s THEN 0 ELSE 1 END,
                    access DESC
                """,
                (user_role, tenant_id, tenant_id),
            )
            rules = cur.fetchall()

        if not rules:
            return f"No permission rules for role '{user_role}' — access denied"

        # Evaluate rules in priority order (tenant-specific before __default__)
        for pattern, access, _rule_tenant in rules:
            if fnmatch.fnmatch(tool_name, pattern):
                if access == "deny":
                    return f"Role '{user_role}' denied '{tool_name}' (pattern: {pattern})"
                return None  # Explicitly allowed

        return f"No permission rule matched for role '{user_role}' on '{tool_name}' — access denied"

    except Exception:
        logger.warning("Permission check failed — denying access", exc_info=True)
        return "Permission check unavailable — access denied"


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
    if not tenant_id:
        return (DEFAULT_TENANT,)

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


def seed_default_permissions() -> None:
    """Insert platform-default role permissions if not already present.

    Called during migrations or first boot.  Uses ``__default__`` as the
    tenant_id so rules apply to all tenants unless overridden.
    """
    from robothor.db.connection import get_connection

    defaults: list[tuple[str, str, str]] = [
        ("viewer", "search_*", "allow"),
        ("viewer", "get_*", "allow"),
        ("viewer", "list_*", "allow"),
        ("viewer", "memory_block_read", "allow"),
        ("viewer", "memory_block_list", "allow"),
        ("viewer", "*", "deny"),
        # user: full access
        ("user", "*", "allow"),
        # admin: full access
        ("admin", "*", "allow"),
        # owner: full access
        ("owner", "*", "allow"),
    ]

    with get_connection() as conn:
        cur = conn.cursor()
        for role, pattern, access in defaults:
            cur.execute(
                """
                INSERT INTO role_permissions (tenant_id, role, tool_pattern, access)
                VALUES ('__default__', %s, %s, %s)
                ON CONFLICT (tenant_id, role, tool_pattern) DO NOTHING
                """,
                (role, pattern, access),
            )
