"""RLS must not hide the global RBAC defaults.

`role_permissions` is not tenant *data*, it is tenant *config*. Its rows are
tagged `tenant_id = '__default__'` and are the built-in rules every tenant
inherits (viewer/owner/admin/user/service). `check_tool_permission` knows this —
it queries `WHERE role = %s AND tenant_id IN (%s, '__default__')`.

Migration 081's `tenant_isolation` policy did not. It admits a row only when its
`tenant_id` equals the connection's `app.tenant_id`, so the moment the engine
began connecting as a *scoped, non-superuser* role it could see **zero** of the
ten rules — and RBAC, correctly denying by default, blocked `search_memory`,
`read_file` and `memory_block_read` across the entire fleet.

The runs still reported `completed`. The only outward sign was a guardrail-event
count: the fleet was running without memory access and calling it success.

Migration 083 grants what was always intended — a tenant sees its own rows PLUS
the global defaults — while still hiding another tenant's overrides.
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[3] / "crm" / "migrations" / "083_rls_global_role_defaults.sql"
)


def _sql() -> str:
    return MIGRATION.read_text()


def test_migration_exists() -> None:
    assert MIGRATION.exists(), "083 must ship — the box has it applied"


def test_policy_admits_the_global_defaults() -> None:
    """Without this clause the engine sees zero rules and RBAC denies everything."""
    sql = _sql()
    assert re.search(r"tenant_id\s*=\s*'__default__'", sql), (
        "the policy must admit tenant_id = '__default__' rows, or a scoped "
        "connection sees none of the built-in role rules"
    )


def test_policy_still_scopes_to_the_connection_tenant() -> None:
    sql = _sql()
    assert "current_setting('app.tenant_id'" in sql
    assert re.search(r"tenant_id\s*=\s*current_setting", sql), (
        "the policy must still admit the connection's own tenant"
    )


def test_writes_are_not_widened_to_the_defaults() -> None:
    """Reading the shared defaults is the point. WRITING them is not.

    A WITH CHECK that allowed `__default__` would let any tenant rewrite the
    rules every other tenant inherits — a privilege-escalation path straight
    through the RBAC table.
    """
    sql = _sql()
    check = sql[sql.index("WITH CHECK") :] if "WITH CHECK" in sql else ""
    assert check, "the policy must carry an explicit WITH CHECK"
    assert "__default__" not in check, (
        "WITH CHECK must NOT admit '__default__': a tenant that can write the "
        "global defaults can grant itself any permission, and every other tenant "
        "inherits it"
    )
