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
    5. No match              →  allowed (fail-open for unconfigured roles)
"""

from __future__ import annotations

import fnmatch
import logging

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
                    access ASC
                """,
                (user_role, tenant_id, tenant_id),
            )
            rules = cur.fetchall()

        if not rules:
            return None  # No rules configured — fail-open

        # Evaluate rules in priority order (tenant-specific before __default__)
        for pattern, access, _rule_tenant in rules:
            if fnmatch.fnmatch(tool_name, pattern):
                if access == "deny":
                    return f"Role '{user_role}' denied '{tool_name}' (pattern: {pattern})"
                return None  # Explicitly allowed

        return None  # No matching rule — fail-open

    except Exception:
        logger.debug("Permission check failed, allowing by default", exc_info=True)
        return None  # Fail-open on errors — don't block operations


def resolve_accessible_tenants(
    tenant_id: str,
    role: str,
) -> tuple[str, ...]:
    """Resolve which tenants a user can access.

    - All users can access their own tenant.
    - ``owner`` and ``admin`` roles in a tenant with ``child_data_access=TRUE``
      can also read data from child tenants.

    Args:
        tenant_id: The user's home tenant.
        role: The user's role.

    Returns:
        Tuple of accessible tenant IDs (always includes own tenant).
    """
    if not tenant_id:
        return (DEFAULT_TENANT,)

    if role not in ("owner", "admin"):
        return (tenant_id,)

    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()

            # Check if this tenant has child_data_access enabled
            cur.execute(
                "SELECT child_data_access FROM crm_tenants WHERE id = %s",
                (tenant_id,),
            )
            row = cur.fetchone()
            if not row or not row[0]:
                return (tenant_id,)

            # Recursive CTE to find all descendant tenants
            cur.execute(
                """
                WITH RECURSIVE children AS (
                    SELECT id FROM crm_tenants
                    WHERE parent_tenant_id = %s AND active = TRUE
                    UNION ALL
                    SELECT t.id FROM crm_tenants t
                    JOIN children c ON t.parent_tenant_id = c.id
                    WHERE t.active = TRUE
                )
                SELECT id FROM children
                """,
                (tenant_id,),
            )
            child_ids = [r[0] for r in cur.fetchall()]
            return (tenant_id, *child_ids)

    except Exception:
        logger.debug("Tenant hierarchy lookup failed", exc_info=True)
        return (tenant_id,)


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
