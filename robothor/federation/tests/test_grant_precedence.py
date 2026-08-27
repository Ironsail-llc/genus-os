"""A per-connection grant beats the role. That is the control path, and it is
the one place the deny-all default can be widened.

`federation_child` is seeded `'*' -> deny`, which is what makes "children have
no control over the parent" the default rather than an opt-in. But
`check_tool_permission` evaluates `user_permissions` FIRST and lets it win
outright — allow or deny. So the deny-all role is a strong default, not a hard
ceiling: one row keyed on `federation:{connection_id}` grants that one peer a
tool.

That is deliberate — an operator running an organisation needs to be able to
give a specific instance a specific capability without widening the role for
every peer at once. It is written down here because the two facts together
("children are deny-all" and "one row overrides it") are exactly the pair that
gets half-remembered, and because a future change to precedence would silently
turn every deliberate grant into a no-op.
"""

from __future__ import annotations

import os
import uuid

import pytest

from robothor.engine.permissions import check_tool_permission

pytestmark = pytest.mark.integration


def _db() -> bool:
    try:
        from robothor.db.connection import get_connection

        with get_connection() as db:
            db.cursor().execute("SELECT 1")
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(
    not os.environ.get("ROBOTHOR_DB_NAME") and not _db(), reason="no database"
)


@pytest.fixture
def grant():
    """Insert a per-connection permission, and take it away afterwards."""
    made: list[str] = []

    def _grant(user_id: str, pattern: str, access: str, tenant: str) -> None:
        from robothor.db.connection import get_connection

        with get_connection() as db:
            cur = db.cursor()
            cur.execute(
                "INSERT INTO user_permissions (tenant_id, user_id, tool_pattern, access) "
                "VALUES (%s, %s, %s, %s)",
                (tenant, user_id, pattern, access),
            )
            db.commit()
        made.append(user_id)

    yield _grant

    if made:
        from robothor.db.connection import get_connection

        with get_connection() as db:
            cur = db.cursor()
            cur.execute("DELETE FROM user_permissions WHERE user_id = ANY(%s)", (made,))
            db.commit()


@pytest.fixture
def tenant():
    from robothor.constants import DEFAULT_TENANT

    return DEFAULT_TENANT


@requires_db
def test_a_child_is_denied_everything_by_default(tenant):
    denial = check_tool_permission(
        "federation_child", tenant, "exec", user_id=f"federation:{uuid.uuid4()}"
    )
    assert denial, "the deny-all seed is missing — every child would be unrestricted"


@requires_db
def test_a_parent_may_read_but_not_execute(tenant):
    principal = f"federation:{uuid.uuid4()}"
    assert (
        check_tool_permission("federation_parent", tenant, "get_stats", user_id=principal) is None
    )
    assert check_tool_permission("federation_parent", tenant, "exec", user_id=principal), (
        "federation_parent is a read-only role; granting execution is a "
        "per-connection decision, not a property of being a parent"
    )


@requires_db
def test_a_per_connection_grant_overrides_the_role(grant, tenant):
    """The operator's control path. If this stops working, every deliberate
    grant becomes a silent no-op and federation looks broken rather than
    locked down."""
    principal = f"federation:{uuid.uuid4()}"
    grant(principal, "exec", "allow", tenant)

    assert check_tool_permission("federation_parent", tenant, "exec", user_id=principal) is None


@requires_db
def test_a_per_connection_grant_can_widen_even_a_deny_all_child(grant, tenant):
    """The sharp edge, stated plainly: `federation_child` is a strong default,
    not a ceiling. A single row gives one child one tool."""
    principal = f"federation:{uuid.uuid4()}"
    grant(principal, "get_stats", "allow", tenant)

    assert check_tool_permission("federation_child", tenant, "get_stats", user_id=principal) is None
    # …and only that tool.
    assert check_tool_permission("federation_child", tenant, "exec", user_id=principal)


@requires_db
def test_a_per_connection_deny_beats_a_role_allow(grant, tenant):
    """Tightening works in the same mechanism, which is what makes it safe to
    suspend one peer's capability without touching the role."""
    principal = f"federation:{uuid.uuid4()}"
    grant(principal, "get_*", "deny", tenant)

    assert check_tool_permission("federation_parent", tenant, "get_stats", user_id=principal)


@requires_db
def test_the_grant_is_scoped_to_one_connection(grant, tenant):
    """A grant to one peer must not leak to the next one paired."""
    granted = f"federation:{uuid.uuid4()}"
    other = f"federation:{uuid.uuid4()}"
    grant(granted, "exec", "allow", tenant)

    assert check_tool_permission("federation_parent", tenant, "exec", user_id=other), (
        "a grant to one connection reached another"
    )
