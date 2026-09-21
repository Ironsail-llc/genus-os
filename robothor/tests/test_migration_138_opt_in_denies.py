"""138 turns the sales tools and web_render into permissions, not advertisements.

Migration 134 inserted ``('__default__','sales_research_agent','web_render','allow')``
under a comment saying "Only the bounded research role gains this permission
automatically". That was not true. 037 seeds ``('__default__','user','*','allow')``
and 107 seeds ``('__default__','service','*','allow')`` — ``service`` being the
role every automated agent run carries by default — so ``web_render`` was
already allowed for every default-role caller and 134 was an additive grant to
a role that also holds a ``*`` deny. The same held for all ten ``sales_*``
tools: neither ``sales_discover`` nor ``sales_propose_email`` sends anything,
but both are unbounded CRM writes.

138 adds the denies that make 134's comment true. ``check_tool_permission``
resolves most-specific-wins with deny breaking ties, so:

* ``web_render`` (exact) beats ``*`` (catch-all) → denied for the broad roles.
* ``sales_*`` (6 literal characters) beats ``*`` → denied for the broad roles.
* An exact allow such as 134's ``web_render`` for ``sales_research_agent``, or
  127's ``sales_discover`` for ``sales_agent``, is per-role and untouched.
* A tenant that wants either back adds a tenant-scoped row, which is evaluated
  before ``__default__`` entirely.

``admin`` and ``owner`` are deliberately NOT denied: those are the operator's
own roles, not an agent's, and the operator must be able to drive their own
sales deployment.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from psycopg2.extras import RealDictCursor

pytestmark = pytest.mark.integration

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "crm" / "migrations"
MIGRATION_138 = MIGRATIONS_DIR / "138_opt_in_tool_denies.sql"

#: The broad roles an automated or ordinary caller actually runs as.
GATED_ROLES = ("service", "user", "member")
#: The operator's own roles, which keep their catch-all allow.
OPERATOR_ROLES = ("admin", "owner")


def test_migration_file_exists():
    assert MIGRATION_138.exists()


def _rules(cursor, role):
    cursor.execute(
        "SELECT tool_pattern, access FROM role_permissions "
        "WHERE tenant_id = '__default__' AND role = %s",
        (role,),
    )
    return {r["tool_pattern"]: r["access"] for r in cursor.fetchall()}


@pytest.mark.parametrize("role", GATED_ROLES)
def test_a_broad_role_loses_the_browser_and_the_sales_tools(scratch_db, role):
    db, _ = scratch_db(through="137_sales_analyst_permissions")
    cursor = db.cursor(cursor_factory=RealDictCursor)
    before = _rules(cursor, role)
    assert before.get("web_render") is None
    assert before.get("sales_*") is None

    scratch_db(through="138_opt_in_tool_denies")

    after = _rules(cursor, role)
    assert after["web_render"] == "deny"
    assert after["sales_*"] == "deny"
    # Only 'service' gets the workflow pump back, and only that one tool.
    assert after.get("sales_process_queue") == ("allow" if role == "service" else None)


@pytest.mark.parametrize("role", OPERATOR_ROLES)
def test_the_operators_own_roles_keep_their_catch_all(scratch_db, role):
    db, _ = scratch_db(through="138_opt_in_tool_denies")
    cursor = db.cursor(cursor_factory=RealDictCursor)

    rules = _rules(cursor, role)

    assert rules["*"] == "allow"
    assert "web_render" not in rules
    assert "sales_*" not in rules


def test_the_reviewed_sales_roles_keep_the_grants_their_manifests_need(scratch_db):
    db, _ = scratch_db(through="138_opt_in_tool_denies")
    cursor = db.cursor(cursor_factory=RealDictCursor)

    assert _rules(cursor, "sales_research_agent")["web_render"] == "allow"
    assert _rules(cursor, "sales_agent")["sales_discover"] == "allow"
    assert _rules(cursor, "sales_agent")["sales_propose_email"] == "allow"


@pytest.mark.parametrize(
    ("role", "tool", "allowed"),
    [
        ("service", "web_render", False),
        ("service", "sales_discover", False),
        ("service", "sales_propose_email", False),
        ("service", "sales_get_workspace", False),
        # The native workflow pump. Its gate is the handler's service-workflow
        # identity check, not RBAC -- see the migration's own note.
        ("service", "sales_process_queue", True),
        ("service", "web_fetch", True),
        ("user", "web_render", False),
        ("user", "sales_discover", False),
        ("owner", "web_render", True),
        ("sales_research_agent", "web_render", True),
        ("sales_research_agent", "sales_discover", False),
        ("sales_agent", "sales_discover", True),
        ("sales_agent", "sales_propose_email", True),
        ("sales_agent", "web_render", False),
    ],
)
def test_the_live_decision_matches_the_rows(scratch_db, monkeypatch, role, tool, allowed):
    """The rows are only as good as what check_tool_permission makes of them."""
    import contextlib

    import psycopg2

    from robothor.engine import permissions

    _, dsn = scratch_db(through="138_opt_in_tool_denies")

    @contextlib.contextmanager
    def connect():
        conn = psycopg2.connect(dsn)
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr("robothor.db.connection.get_connection", connect)

    reason = permissions.check_tool_permission(role, "tenant-under-test", tool)

    assert (reason is None) is allowed, reason
