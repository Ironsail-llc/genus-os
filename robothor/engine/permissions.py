"""Per-user permission enforcement for Genus OS.

Checks whether a user's role permits a given tool call, and resolves
which tenants a user can access based on the tenant hierarchy.

Every tool call must carry a concrete role.  Automated runs use the agent's
explicit ``service_role``; a missing role is denied instead of being treated as
an implicit all-powerful system identity.

Permission rules live in the ``role_permissions`` database table, with
a ``__default__`` tenant providing platform-wide defaults that any
tenant can override.

Evaluation order:
    1. Tenant-specific matching rules (most-specific pattern wins; DENY wins ties)
    2. ``__default__`` matching rules (same specificity rule)
    3. No match → denied (fail-closed for unconfigured roles)

Specificity matters because read-only roles use explicit allow patterns such as
``search_*`` plus a catch-all ``*`` deny.  Treating the catch-all deny as the
first match would accidentally block the entire allowlist.

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
        user_role: The user's role (viewer, user, admin, owner, service).
            Empty strings are invalid and fail closed.
        tenant_id: The tenant to check permissions for.
        tool_name: The tool being invoked.

    Returns:
        Denial reason string, or None if allowed.
    """
    if not user_role:
        return "Missing execution role — access denied"

    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()

            # Fetch both policy levels.  Precedence is evaluated below rather
            # than delegated to SQL ordering because pattern specificity must
            # beat a catch-all rule of the opposite access type.
            cur.execute(
                """
                SELECT tool_pattern, access, tenant_id
                FROM role_permissions
                WHERE role = %s AND tenant_id IN (%s, '__default__')
                """,
                (user_role, tenant_id),
            )
            rules = cur.fetchall()

        if not rules:
            return f"No permission rules for role '{user_role}' — access denied"

        # Evaluate tenant-specific policy before platform defaults.  Within a
        # level, choose the most-specific glob; an exact/specific allow can
        # therefore carve permitted operations out of a catch-all deny while
        # an equally specific deny remains authoritative.
        for policy_tenant in (tenant_id, "__default__"):
            matching = [
                (pattern, access)
                for pattern, access, rule_tenant in rules
                if rule_tenant == policy_tenant and fnmatch.fnmatch(tool_name, pattern)
            ]
            if not matching:
                continue
            pattern, access = max(
                matching,
                key=lambda rule: (
                    rule[0] == tool_name,
                    sum(character not in "*?[]" for character in rule[0]),
                    rule[1] == "deny",
                ),
            )
            if access == "deny":
                return f"Role '{user_role}' denied '{tool_name}' (pattern: {pattern})"
            return None

        return f"No permission rule matched for role '{user_role}' on '{tool_name}' — access denied"

    except Exception:
        logger.warning("Permission check failed — denying access", exc_info=True)
        return "Permission check unavailable — access denied"


def classify_system_tool_access(
    service_role: str,
    tenant_id: str,
    tool_name: str,
    mode: str,
) -> tuple[str, str | None]:
    """Decide what to do with a SYSTEM-run (cron/hook/heartbeat) tool call.

    Interactive runs are gated by the dispatch ``user_role`` check; system runs
    have no interactive user, so this applies the agent's ``service_role`` under
    the ``rbac_enforcement_mode`` ladder.

    Returns ``(action, reason)`` where action is:
      * ``"allow"`` — permitted (or mode is off — no check performed)
      * ``"block"`` — denied AND mode is ``enforce`` (caller blocks the tool)
      * ``"observe"`` — denied but mode is ``observe``/``alert`` (caller logs
        the would-deny and ALLOWS the tool, preserving behavior)
    """
    if mode == "off":
        return ("allow", None)
    reason = check_tool_permission(service_role, tenant_id, tool_name)
    if reason is None:
        return ("allow", None)
    if mode == "enforce":
        return ("block", reason)
    return ("observe", reason)


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
        # service: system/cron/heartbeat runs (no interactive user). Explicitly
        # allow all by default; operators can replace this with narrower rules.
        ("service", "*", "allow"),
        # user: full access
        ("user", "*", "allow"),
        # member: canonical human role used by account-backed sessions
        ("member", "*", "allow"),
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
