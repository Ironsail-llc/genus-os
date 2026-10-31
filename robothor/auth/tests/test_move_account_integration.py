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
from typing import TYPE_CHECKING, Any

import pytest

from robothor.auth import accounts

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

#: One seeded row per linked table, except tenant_users: the person's routing
#: row and the Telegram pairing row (keyed by the account, no person_id).
EXPECTED_COUNTS = {
    **dict.fromkeys(accounts.MOVED_TABLES, 1),
    "tenant_users": 2,
    "contact_identifiers": 0,  # the world seeds none; see the user-add RLS test
}

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
def world(monkeypatch) -> Iterator[dict[str, Any]]:
    """acme-parent (root) with alice@example.com and one row per linked table;
    acme-child-a beneath it. The shell is the root's. Everything is removed
    afterwards."""
    sfx = uuid.uuid4().hex[:8]
    parent, child = f"acme-parent-{sfx}", f"acme-child-a-{sfx}"
    monkeypatch.setenv("ROBOTHOR_TENANT_ID", parent)
    conn = _connect()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("INSERT INTO crm_tenants (id, display_name) VALUES (%s, 'Acme Parent')", (parent,))
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
    # The shape a Telegram pairing writes (channels/identities.py
    # _upsert_tenant_user): keyed by the account's id, with no person_id.
    cur.execute(
        "INSERT INTO tenant_users (telegram_user_id, display_name, tenant_id, role, user_id) "
        "VALUES (%s, 'Alice', %s, 'member', %s)",
        (f"tg-pair-{sfx}", parent, user_id),
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
        cur.execute("DELETE FROM audit_log WHERE target = %s", (f"user_account:{user_id}",))
        cur.execute("DELETE FROM user_sessions WHERE user_id = %s", (user_id,))
        for table in (
            "sso_binding_grants",
            "tenant_users",
            "face_identities",
            "user_channel_identities",
            "user_permissions",
            "user_accounts",
            "crm_notes",
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
        "tenant_users (pairing)": ("SELECT tenant_id FROM tenant_users WHERE user_id = %s", uid),
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
    report = accounts.move_account(world["user_id"], world["child"], actor="cli:test", role="admin")

    assert set(_where(cur, world).values()) == {world["child"]}
    assert report.counts == EXPECTED_COUNTS
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


def test_the_audit_row_is_filed_under_the_target_tenant(world) -> None:
    """``audit_log.tenant_id`` (migration 162, from the tenant admin gates
    work) is how a tenant's own trail is read. Skipped where that column has
    not been migrated yet."""
    cur = world["cur"]
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'audit_log' AND column_name = 'tenant_id'"
    )
    if cur.fetchone() is None:
        pytest.skip("audit_log.tenant_id is not migrated on this database")
    accounts.move_account(world["user_id"], world["child"], actor="cli:test")
    cur.execute(
        "SELECT tenant_id FROM audit_log WHERE target = %s",
        (f"user_account:{world['user_id']}",),
    )
    assert [r[0] for r in cur.fetchall()] == [world["child"]]


def test_a_shell_bound_to_the_child_is_refused_and_moves_nothing(world, monkeypatch) -> None:
    """The move is the root operator's, as for ``move-workload``."""
    monkeypatch.setenv("ROBOTHOR_TENANT_ID", world["child"])
    cur = world["cur"]
    with pytest.raises(accounts.AccountMoveError, match="root") as exc:
        accounts.move_account(world["user_id"], world["child"], actor="cli:test")
    assert world["parent"] in str(exc.value)
    assert set(_where(cur, world).values()) == {world["parent"]}
    assert _audit_rows(cur, world["user_id"]) == []


def test_dry_run_counts_and_moves_nothing(world) -> None:
    cur = world["cur"]
    report = accounts.move_account(world["user_id"], world["child"], actor="cli:test", dry_run=True)
    assert report.counts == EXPECTED_COUNTS
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
    cur.execute("DELETE FROM user_permissions WHERE tenant_id = %s", (world["child"],))

    assert set(_where(cur, world).values()) == {world["parent"]}
    cur.execute("SELECT role FROM user_accounts WHERE id = %s", (world["user_id"],))
    assert cur.fetchone()[0] == "member"
    assert _live_sessions(cur, world["user_id"]) == 1
    assert _audit_rows(cur, world["user_id"]) == []


def test_accounts_for_email_finds_the_account_across_tenants(world) -> None:
    found = [
        a
        for a in accounts.accounts_for_email("Alice@Example.com")
        if a["tenant_id"] in (world["parent"], world["child"])
    ]
    assert [a["tenant_id"] for a in found] == [world["parent"]]
    assert "password_hash" not in found[0]


# ─── the person's CRM history ────────────────────────────────────────────


def _seed_note(w: dict[str, Any]) -> str:
    cur = w["cur"]
    cur.execute(
        "INSERT INTO crm_notes (tenant_id, title, person_id) VALUES (%s, 'Call', %s) RETURNING id",
        (w["parent"], w["person_id"]),
    )
    return str(cur.fetchone()[0])


def test_a_person_with_crm_history_is_not_moved_by_default(world) -> None:
    """The person row is the source tenant's CRM contact. Moving it would leave
    the tenant's notes pointing at someone it can no longer see."""
    _seed_note(world)
    cur = world["cur"]
    with pytest.raises(accounts.AccountMoveError, match=r"crm_notes=1.*--detach-person"):
        accounts.move_account(world["user_id"], world["child"], actor="cli:test")
    assert set(_where(cur, world).values()) == {world["parent"]}


def test_a_face_enrolment_not_tied_to_the_account_counts_as_history(world) -> None:
    """The account's own enrolment moves with it; another enrolment of the same
    person (no user_account_id) belongs to the source tenant's records."""
    cur = world["cur"]
    cur.execute(
        "INSERT INTO face_identities (tenant_id, face_label, person_id) VALUES (%s, %s, %s)",
        (world["parent"], f"alice-door-{uuid.uuid4().hex[:6]}", world["person_id"]),
    )
    with pytest.raises(accounts.AccountMoveError, match=r"face_identities=1"):
        accounts.move_account(world["user_id"], world["child"], actor="cli:test")
    assert set(_where(cur, world).values()) == {world["parent"]}


def test_the_accounts_own_face_enrolment_is_not_history(world) -> None:
    """The seeded enrolment is tied to the account, so the move goes ahead."""
    accounts.move_account(world["user_id"], world["child"], actor="cli:test")
    assert set(_where(world["cur"], world).values()) == {world["child"]}


def test_a_dry_run_reports_the_persons_crm_history(world) -> None:
    _seed_note(world)
    with pytest.raises(accounts.AccountMoveError, match=r"crm_notes=1"):
        accounts.move_account(world["user_id"], world["child"], actor="cli:test", dry_run=True)


def test_detach_person_leaves_the_contact_and_gives_the_account_a_new_one(world) -> None:
    note_id = _seed_note(world)
    cur = world["cur"]
    report = accounts.move_account(
        world["user_id"], world["child"], actor="cli:test", detach_person=True
    )

    # The old contact, and its history, stay where they were.
    cur.execute("SELECT tenant_id FROM crm_people WHERE id = %s", (world["person_id"],))
    assert cur.fetchone()[0] == world["parent"]
    cur.execute("SELECT tenant_id, person_id FROM crm_notes WHERE id = %s", (note_id,))
    tenant, person = cur.fetchone()
    assert (tenant, str(person)) == (world["parent"], world["person_id"])

    # The account, its Telegram routing row and its face enrolment point at a
    # fresh contact in the target tenant.
    cur.execute("SELECT tenant_id, person_id FROM user_accounts WHERE id = %s", (world["user_id"],))
    tenant, new_person = cur.fetchone()
    assert tenant == world["child"]
    assert new_person is not None and str(new_person) != world["person_id"]
    cur.execute("SELECT tenant_id, first_name, email FROM crm_people WHERE id = %s", (new_person,))
    assert cur.fetchone() == (world["child"], "Alice", "alice@example.com")
    cur.execute("SELECT tenant_id FROM tenant_users WHERE person_id = %s", (new_person,))
    assert [r[0] for r in cur.fetchall()] == [world["child"]]
    cur.execute(
        "SELECT tenant_id, person_id FROM face_identities WHERE user_account_id = %s",
        (world["user_id"],),
    )
    assert cur.fetchone() == (world["child"], new_person)

    assert report.new_person_id == str(new_person)
    assert report.counts["crm_people"] == 1


# ─── the role follows into channel identities ────────────────────────────


def _channel_role(w: dict[str, Any]) -> str:
    w["cur"].execute("SELECT role FROM user_channel_identities WHERE user_id = %s", (w["user_id"],))
    return str(w["cur"].fetchone()[0])


def test_a_demotion_to_viewer_caps_the_channel_identity(world) -> None:
    accounts.move_account(world["user_id"], world["child"], actor="cli:test", role="viewer")
    assert _channel_role(world) == "viewer"


def test_a_role_change_never_raises_a_channel_identity(world) -> None:
    world["cur"].execute(
        "UPDATE user_channel_identities SET role = 'viewer' WHERE user_id = %s",
        (world["user_id"],),
    )
    accounts.move_account(world["user_id"], world["child"], actor="cli:test", role="admin")
    assert _channel_role(world) == "viewer"


def test_no_role_change_leaves_the_channel_identity_alone(world) -> None:
    accounts.move_account(world["user_id"], world["child"], actor="cli:test")
    assert _channel_role(world) == "member"


def _pairing(w: dict[str, Any]) -> tuple[str, str]:
    w["cur"].execute("SELECT tenant_id, role FROM tenant_users WHERE user_id = %s", (w["user_id"],))
    tenant, role = w["cur"].fetchone()
    return str(tenant), str(role)


def test_the_telegram_pairing_moves_with_the_capped_role(world) -> None:
    """Telegram's inbound path reads only tenant_users; a pairing left behind
    would keep driving the source tenant's agent."""
    accounts.move_account(world["user_id"], world["child"], actor="cli:test", role="viewer")
    assert _pairing(world) == (world["child"], "viewer")


def test_the_telegram_pairing_keeps_its_role_without_a_demotion(world) -> None:
    accounts.move_account(world["user_id"], world["child"], actor="cli:test", role="admin")
    assert _pairing(world) == (world["child"], "member")


# ─── the contact's company ───────────────────────────────────────────────


def test_a_company_left_in_the_source_is_unlinked_and_reported(world) -> None:
    cur = world["cur"]
    cur.execute(
        "INSERT INTO crm_companies (tenant_id, name) VALUES (%s, 'Example Co') RETURNING id",
        (world["parent"],),
    )
    company_id = str(cur.fetchone()[0])
    cur.execute(
        "UPDATE crm_people SET company_id = %s WHERE id = %s", (company_id, world["person_id"])
    )
    try:
        dry = accounts.move_account(
            world["user_id"], world["child"], actor="cli:test", dry_run=True
        )
        assert dry.company_unlinked == company_id

        report = accounts.move_account(world["user_id"], world["child"], actor="cli:test")
        assert report.company_unlinked == company_id
        cur.execute(
            "SELECT tenant_id, company_id FROM crm_people WHERE id = %s", (world["person_id"],)
        )
        assert cur.fetchone() == (world["child"], None)
        cur.execute("SELECT tenant_id FROM crm_companies WHERE id = %s", (company_id,))
        assert cur.fetchone()[0] == world["parent"]
    finally:
        cur.execute("UPDATE crm_people SET company_id = NULL WHERE id = %s", (world["person_id"],))
        cur.execute("DELETE FROM crm_companies WHERE id = %s", (company_id,))


def test_a_contact_without_a_foreign_company_reports_nothing(world) -> None:
    report = accounts.move_account(world["user_id"], world["child"], actor="cli:test")
    assert report.company_unlinked is None
