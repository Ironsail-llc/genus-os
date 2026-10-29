"""``move_account`` against real Postgres: one transaction, every linked row.

Seeds one row in each table that hangs off an account, moves the account, and
checks every row followed it — and that a failure part-way leaves all of them
where they were. A mock cannot show either: the guarantee is the database's
transaction, not the code's ordering.

Seeds and cleans up through its own committed connection, under tenant ids
suffixed per test, because ``move_account`` opens and commits its own
transaction and a per-test rollback fixture could not observe that.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from robothor.auth import accounts

pytestmark = pytest.mark.integration

PG_DSN = os.environ.get(
    "ROBOTHOR_TEST_DB_DSN",
    "dbname=robothor_test user=robothor host=/var/run/postgresql",
)


def _connect() -> Any:
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        conn = psycopg2.connect(PG_DSN)
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"no test database reachable at ROBOTHOR_TEST_DB_DSN: {exc}")
    if not conn.get_dsn_parameters().get("dbname", "").endswith("_test"):
        pytest.skip("ROBOTHOR_TEST_DB_DSN must name a *_test database")
    return conn


@pytest.fixture
def world() -> Iterator[dict[str, Any]]:
    """acme-parent (root) with alice@example.com and one row per linked table;
    acme-child-a beneath it. Everything is removed afterwards."""
    sfx = uuid.uuid4().hex[:8]
    parent, child = f"acme-parent-{sfx}", f"acme-child-a-{sfx}"
    conn = _connect()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO crm_tenants (id, display_name) VALUES (%s, 'Acme Parent')", (parent,)
    )
    cur.execute(
        "INSERT INTO crm_tenants (id, display_name, parent_tenant_id) "
        "VALUES (%s, 'Acme Child A', %s)",
        (child, parent),
    )
    cur.execute(
        "INSERT INTO crm_people (tenant_id, first_name, last_name, email) "
        "VALUES (%s, 'Alice', 'Example', 'alice@example.com') RETURNING id",
        (parent,),
    )
    person_id = str(cur.fetchone()[0])
    cur.execute(
        "INSERT INTO user_accounts (tenant_id, email, display_name, role, status, person_id) "
        "VALUES (%s, 'alice@example.com', 'Alice', 'member', 'active', %s) RETURNING id",
        (parent, person_id),
    )
    user_id = str(cur.fetchone()[0])
    cur.execute(
        "INSERT INTO user_permissions (tenant_id, user_id, tool_pattern, access) "
        "VALUES (%s, %s, 'web_*', 'allow')",
        (parent, user_id),
    )
    cur.execute(
        "INSERT INTO user_channel_identities "
        "(tenant_id, user_id, channel, native_id, role, paired_by) "
        "VALUES (%s, %s, 'slack', %s, 'member', 'cli:test')",
        (parent, user_id, f"U{sfx}"),
    )
    cur.execute(
        "INSERT INTO face_identities (tenant_id, face_label, person_id, user_account_id) "
        "VALUES (%s, %s, %s, %s)",
        (parent, f"alice-{sfx}", person_id, user_id),
    )
    cur.execute(
        "INSERT INTO tenant_users (telegram_user_id, display_name, tenant_id, role, person_id) "
        "VALUES (%s, 'Alice', %s, 'user', %s)",
        (f"tg-{sfx}", parent, person_id),
    )
    cur.execute(
        "INSERT INTO sso_binding_grants (tenant_id, email, created_by, expires_at) "
        "VALUES (%s, 'alice@example.com', 'cli', NOW() + interval '1 hour')",
        (parent,),
    )
    cur.execute(
        "INSERT INTO user_sessions (user_id, refresh_token_hash, expires_at) "
        "VALUES (%s, %s, NOW() + interval '1 day')",
        (user_id, f"hash-{sfx}"),
    )
    try:
        yield {
            "parent": parent,
            "child": child,
            "user_id": user_id,
            "person_id": person_id,
            "cur": cur,
        }
    finally:
        tenants = (parent, child)
        cur.execute(
            "DELETE FROM audit_log WHERE target = %s", (f"user_account:{user_id}",)
        )
        cur.execute("DELETE FROM user_sessions WHERE user_id = %s", (user_id,))
        for table in (
            "sso_binding_grants",
            "tenant_users",
            "face_identities",
            "user_channel_identities",
            "user_permissions",
            "user_accounts",
            "crm_people",
        ):
            cur.execute(f"DELETE FROM {table} WHERE tenant_id IN %s", (tenants,))  # noqa: S608
        cur.execute("DELETE FROM crm_tenants WHERE id IN %s", ((child, parent),))
        conn.close()


def _where(cur: Any, w: dict[str, Any]) -> dict[str, str]:
    """The tenant each seeded row is in now."""
    uid, pid = w["user_id"], w["person_id"]
    probes = {
        "user_accounts": ("SELECT tenant_id FROM user_accounts WHERE id = %s", uid),
        "crm_people": ("SELECT tenant_id FROM crm_people WHERE id = %s", pid),
        "user_permissions": ("SELECT tenant_id FROM user_permissions WHERE user_id = %s", uid),
        "user_channel_identities": (
            "SELECT tenant_id FROM user_channel_identities WHERE user_id = %s",
            uid,
        ),
        "face_identities": (
            "SELECT tenant_id FROM face_identities WHERE user_account_id = %s",
            uid,
        ),
        "tenant_users": ("SELECT tenant_id FROM tenant_users WHERE person_id = %s", pid),
        "sso_binding_grants": (
            "SELECT tenant_id FROM sso_binding_grants WHERE email = 'alice@example.com' "
            "AND tenant_id IN (%s, %s)",
            None,
        ),
    }
    out: dict[str, str] = {}
    for table, (sql, arg) in probes.items():
        cur.execute(sql, (w["parent"], w["child"]) if arg is None else (arg,))
        rows = cur.fetchall()
        assert len(rows) == 1, f"{table}: expected one seeded row, found {len(rows)}"
        out[table] = rows[0][0]
    return out


def _live_sessions(cur: Any, user_id: str) -> int:
    cur.execute(
        "SELECT COUNT(*) FROM user_sessions WHERE user_id = %s AND revoked_at IS NULL",
        (user_id,),
    )
    return int(cur.fetchone()[0])


def _audit_rows(cur: Any, user_id: str) -> list[Any]:
    cur.execute(
        "SELECT actor, details FROM audit_log WHERE target = %s",
        (f"user_account:{user_id}",),
    )
    return cur.fetchall()


def test_live_move_moves_every_linked_row_revokes_sessions_and_audits(world) -> None:
    cur = world["cur"]
    report = accounts.move_account(
        world["user_id"], world["child"], actor="cli:test", role="admin"
    )

    assert set(_where(cur, world).values()) == {world["child"]}
    assert report.counts == dict.fromkeys(accounts.MOVED_TABLES, 1)
    assert report.sessions_revoked == 1
    assert _live_sessions(cur, world["user_id"]) == 0

    cur.execute("SELECT role FROM user_accounts WHERE id = %s", (world["user_id"],))
    assert cur.fetchone()[0] == "admin"

    audit = _audit_rows(cur, world["user_id"])
    assert len(audit) == 1
    actor, details = audit[0]
    assert actor == "cli:test"
    assert details["from_tenant"] == world["parent"]
    assert details["to_tenant"] == world["child"]


def test_dry_run_counts_and_moves_nothing(world) -> None:
    cur = world["cur"]
    report = accounts.move_account(
        world["user_id"], world["child"], actor="cli:test", dry_run=True
    )
    assert report.counts == dict.fromkeys(accounts.MOVED_TABLES, 1)
    assert set(_where(cur, world).values()) == {world["parent"]}
    assert _live_sessions(cur, world["user_id"]) == 1
    assert _audit_rows(cur, world["user_id"]) == []


def test_a_failure_part_way_leaves_every_table_untouched(world) -> None:
    """A permission row already in the target collides with the moving one.

    ``user_accounts`` and ``crm_people`` have already been rewritten by the time
    the ``user_permissions`` UPDATE hits the unique constraint, so this proves
    the earlier writes are undone, not merely never attempted.
    """
    cur = world["cur"]
    cur.execute(
        "INSERT INTO user_permissions (tenant_id, user_id, tool_pattern, access) "
        "VALUES (%s, %s, 'web_*', 'deny')",
        (world["child"], world["user_id"]),
    )
    import psycopg2

    with pytest.raises(psycopg2.IntegrityError):
        accounts.move_account(world["user_id"], world["child"], actor="cli:test")
    cur.execute(
        "DELETE FROM user_permissions WHERE tenant_id = %s", (world["child"],)
    )

    assert set(_where(cur, world).values()) == {world["parent"]}
    cur.execute("SELECT role FROM user_accounts WHERE id = %s", (world["user_id"],))
    assert cur.fetchone()[0] == "member"
    assert _live_sessions(cur, world["user_id"]) == 1
    assert _audit_rows(cur, world["user_id"]) == []


def test_accounts_for_email_finds_the_account_across_tenants(world) -> None:
    found = [a for a in accounts.accounts_for_email("Alice@Example.com") if a["tenant_id"] in (world["parent"], world["child"])]
    assert [a["tenant_id"] for a in found] == [world["parent"]]
    assert "password_hash" not in found[0]
