"""``tenant_plugin_grants`` — the root tenant's per-child plugin decisions.

A root operator may force a plugin ``on`` or ``off`` for one child tenant. The
record is a table of its own rather than a key in ``crm_tenants.settings``: a
child admin may PATCH its own settings, and a save replaces the whole object,
so a grant stored there would be one the child could delete.

The unit half pins the DAL contract without a database: bad arguments are
refused before any SQL runs, and every statement is scoped to the tenant. The
integration half proves it on PostgreSQL: a grant round-trips, a second write
replaces the first, clearing removes it, and two tenants never see each
other's grants.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "crm" / "migrations"
MANIFEST = Path(__file__).resolve().parents[2] / "migrations" / "manifest.txt"
MIGRATION = "160_tenant_plugin_grants"


# ─── unit: the contract, no database ─────────────────────────────────────


def _recording_connect(fetchall: list[Any] | None = None, rowcount: int = 0):
    cur = MagicMock()
    cur.fetchall.return_value = fetchall or []
    cur.rowcount = rowcount
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur

    @contextlib.contextmanager
    def connect():
        yield conn

    return connect, cur


def test_the_states_are_on_and_off():
    from robothor.plugins.grants import GRANT_STATES

    assert set(GRANT_STATES) == {"on", "off"}


def test_get_grants_maps_plugin_to_state_for_one_tenant():
    from robothor.plugins.grants import get_grants

    connect, cur = _recording_connect(fetchall=[("acme-tools", "off"), ("acme-extra", "on")])
    assert get_grants("acme-child-a", connect=connect) == {
        "acme-tools": "off",
        "acme-extra": "on",
    }
    sql, params = cur.execute.call_args.args
    assert "tenant_plugin_grants" in sql
    assert "tenant_id = %s" in sql
    assert params == ("acme-child-a",)


@pytest.mark.parametrize("state", ["", "enabled", "ON", None, True])
def test_set_grant_refuses_a_state_that_is_not_on_or_off(state):
    from robothor.plugins.grants import set_grant

    connect, cur = _recording_connect()
    with pytest.raises(ValueError):
        set_grant("acme-child-a", "acme-tools", state, "operator:alice", connect=connect)
    cur.execute.assert_not_called()


@pytest.mark.parametrize(("tenant", "plugin"), [("", "acme-tools"), ("acme-child-a", " ")])
def test_set_grant_refuses_an_empty_tenant_or_plugin(tenant, plugin):
    from robothor.plugins.grants import set_grant

    connect, cur = _recording_connect()
    with pytest.raises(ValueError):
        set_grant(tenant, plugin, "off", "operator:alice", connect=connect)
    cur.execute.assert_not_called()


def test_set_grant_upserts_and_records_who_set_it():
    from robothor.plugins.grants import set_grant

    connect, cur = _recording_connect()
    set_grant("acme-child-a", "acme-tools", "off", "operator:alice", connect=connect)
    sql, params = cur.execute.call_args.args
    assert "INSERT INTO tenant_plugin_grants" in sql
    assert "ON CONFLICT (tenant_id, plugin)" in sql
    assert params[:4] == ("acme-child-a", "acme-tools", "off", "operator:alice")


def test_a_plugin_name_is_stored_in_canonical_form():
    """``Acme_Tools`` and ``acme-tools`` are one distribution (PEP 503)."""
    from robothor.plugins.grants import clear_grant, get_grants, set_grant

    connect, cur = _recording_connect()
    set_grant("acme-child-a", "Acme_Tools", "off", "operator:alice", connect=connect)
    assert cur.execute.call_args.args[1][1] == "acme-tools"

    connect, cur = _recording_connect(rowcount=1)
    clear_grant("acme-child-a", "Acme.Tools", connect=connect)
    assert cur.execute.call_args.args[1][1] == "acme-tools"

    connect, _cur = _recording_connect(fetchall=[("Acme_Tools", "on")])
    assert get_grants("acme-child-a", connect=connect) == {"acme-tools": "on"}


@pytest.mark.parametrize("fn", ["set_grant", "clear_grant"])
def test_a_write_binds_to_the_root_tenant_not_the_child(fn, monkeypatch):
    """Grants are the root's authority, and RLS lets only the root write them."""
    from robothor import constants
    from robothor.plugins import grants

    bound: list[str] = []
    connect, _cur = _recording_connect(rowcount=1)

    def fake_platform_connect(tenant_id):
        bound.append(tenant_id)
        return connect

    monkeypatch.setattr(grants, "_platform_connect", fake_platform_connect)
    args = (
        ("acme-child-a", "acme-tools", "off", "operator:alice")
        if fn == "set_grant"
        else (
            "acme-child-a",
            "acme-tools",
        )
    )
    getattr(grants, fn)(*args, root_tenant="acme-parent")
    getattr(grants, fn)(*args)
    assert bound == ["acme-parent", constants.PLATFORM_TENANT]


def test_clear_grant_says_whether_a_row_went():
    from robothor.plugins.grants import clear_grant

    connect, cur = _recording_connect(rowcount=1)
    assert clear_grant("acme-child-a", "acme-tools", connect=connect) is True
    sql, params = cur.execute.call_args.args
    assert sql.startswith("DELETE FROM tenant_plugin_grants")
    assert params == ("acme-child-a", "acme-tools")

    connect, _cur = _recording_connect(rowcount=0)
    assert clear_grant("acme-child-a", "acme-tools", connect=connect) is False


def test_migration_file_exists_and_is_in_the_manifest():
    assert (MIGRATIONS_DIR / f"{MIGRATION}.sql").exists()
    assert f"crm/{MIGRATION}.sql" in MANIFEST.read_text().splitlines()


def test_the_migration_lets_only_the_root_write():
    sql = (MIGRATIONS_DIR / f"{MIGRATION}.sql").read_text()
    assert "AS RESTRICTIVE" in sql
    for verb in ("INSERT", "UPDATE", "DELETE"):
        assert f"FOR {verb}" in sql
    assert "tenant_plugin_grants_root" in sql


def test_the_migration_constrains_the_state_and_policies_the_table():
    sql = (MIGRATIONS_DIR / f"{MIGRATION}.sql").read_text()
    assert "PRIMARY KEY (tenant_id, plugin)" in sql
    assert "CHECK (state IN ('on', 'off'))" in sql
    assert "CREATE POLICY tenant_isolation ON tenant_plugin_grants" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql


# ─── integration: the same promises on PostgreSQL ────────────────────────


@pytest.fixture
def pg(scratch_db):
    import psycopg2

    db, dsn = scratch_db(through=MIGRATION)

    @contextlib.contextmanager
    def connect():
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        try:
            yield conn
        finally:
            conn.close()

    return db, connect


@pytest.mark.integration
def test_a_grant_round_trips_and_a_second_write_replaces_it(pg):
    from robothor.plugins.grants import get_grants, set_grant

    db, connect = pg
    assert get_grants("acme-child-a", connect=connect) == {}

    set_grant("acme-child-a", "acme-tools", "off", "operator:alice", connect=connect)
    assert get_grants("acme-child-a", connect=connect) == {"acme-tools": "off"}

    set_grant("acme-child-a", "acme-tools", "on", "operator:bob", connect=connect)
    assert get_grants("acme-child-a", connect=connect) == {"acme-tools": "on"}
    with db.cursor() as cur:
        cur.execute(
            "SELECT set_by, set_at IS NOT NULL FROM tenant_plugin_grants "
            "WHERE tenant_id = 'acme-child-a' AND plugin = 'acme-tools'"
        )
        assert cur.fetchone() == ("operator:bob", True)


@pytest.mark.integration
def test_clearing_a_grant_removes_it(pg):
    from robothor.plugins.grants import clear_grant, get_grants, set_grant

    _db, connect = pg
    set_grant("acme-child-a", "acme-tools", "off", "operator:alice", connect=connect)
    assert clear_grant("acme-child-a", "acme-tools", connect=connect) is True
    assert get_grants("acme-child-a", connect=connect) == {}
    assert clear_grant("acme-child-a", "acme-tools", connect=connect) is False


@pytest.mark.integration
def test_two_tenants_never_see_each_others_grants(pg):
    from robothor.plugins.grants import get_grants, set_grant

    _db, connect = pg
    set_grant("acme-child-a", "acme-tools", "off", "operator:alice", connect=connect)
    set_grant("acme-child-b", "acme-tools", "on", "operator:alice", connect=connect)
    assert get_grants("acme-child-a", connect=connect) == {"acme-tools": "off"}
    assert get_grants("acme-child-b", connect=connect) == {"acme-tools": "on"}


@pytest.mark.integration
def test_the_database_refuses_a_state_the_dal_would_not_write(pg):
    import psycopg2

    db, _connect = pg
    with pytest.raises(psycopg2.errors.CheckViolation), db.cursor() as cur:
        cur.execute(
            "INSERT INTO tenant_plugin_grants (tenant_id, plugin, state, set_by) "
            "VALUES ('acme-child-a', 'acme-tools', 'maybe', 'operator:alice')"
        )


# ─── integration: only the root writes, under real RLS ──────────────────


@pytest.fixture
def rls(scratch_db):
    """The scratch database seen as ``robothor_app`` (no BYPASSRLS), with a
    root and one child, and a way to act as a given tenant."""
    import psycopg2

    db, dsn = scratch_db(through=MIGRATION)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO crm_tenants (id, display_name, parent_tenant_id) VALUES "
            "('acme-parent', 'Acme Parent', NULL), ('acme-child-a', 'Acme Child A', 'acme-parent')"
        )

    def as_tenant(tenant: str | None):
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET ROLE robothor_app")
            cur.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant or "",))
        return conn

    return db, as_tenant


def _row(db, tenant: str = "acme-child-a"):
    with db.cursor() as cur:
        cur.execute(
            "SELECT state FROM tenant_plugin_grants WHERE tenant_id = %s AND plugin = 'acme-tools'",
            (tenant,),
        )
        found = cur.fetchone()
    return None if found is None else found[0]


_INSERT = (
    "INSERT INTO tenant_plugin_grants (tenant_id, plugin, state, set_by) "
    "VALUES ('acme-child-a', 'acme-tools', 'off', 'operator:alice')"
)


@pytest.mark.integration
def test_the_root_can_write_a_childs_grant(rls):
    db, as_tenant = rls
    conn = as_tenant("acme-parent")
    with conn.cursor() as cur:
        cur.execute(_INSERT)
        cur.execute("UPDATE tenant_plugin_grants SET state = 'on' WHERE tenant_id = 'acme-child-a'")
    assert _row(db) == "on"
    with conn.cursor() as cur:
        cur.execute("DELETE FROM tenant_plugin_grants WHERE tenant_id = 'acme-child-a'")
    assert _row(db) is None


@pytest.mark.integration
def test_the_child_reads_its_own_grant(rls):
    db, as_tenant = rls
    with db.cursor() as cur:
        cur.execute(_INSERT)
    with as_tenant("acme-child-a").cursor() as cur:
        cur.execute("SELECT plugin, state FROM tenant_plugin_grants")
        assert cur.fetchall() == [("acme-tools", "off")]


@pytest.mark.integration
def test_the_child_cannot_insert_its_own_grant(rls):
    import psycopg2

    db, as_tenant = rls
    with (
        pytest.raises(psycopg2.errors.InsufficientPrivilege),
        as_tenant("acme-child-a").cursor() as cur,
    ):
        cur.execute(_INSERT)
    assert _row(db) is None


@pytest.mark.integration
def test_the_child_cannot_change_or_delete_its_own_grant(rls):
    db, as_tenant = rls
    with db.cursor() as cur:
        cur.execute(_INSERT)
    with as_tenant("acme-child-a").cursor() as cur:
        cur.execute("UPDATE tenant_plugin_grants SET state = 'on' WHERE tenant_id = 'acme-child-a'")
        assert cur.rowcount == 0
        cur.execute("DELETE FROM tenant_plugin_grants WHERE tenant_id = 'acme-child-a'")
        assert cur.rowcount == 0
    assert _row(db) == "off"


@pytest.mark.integration
def test_an_unbound_connection_can_still_write(rls):
    """The migration runner and an admin shell are unbound; they keep working."""
    db, as_tenant = rls
    with as_tenant(None).cursor() as cur:
        cur.execute(_INSERT)
    assert _row(db) == "off"


@pytest.mark.integration
def test_the_dal_writes_as_the_root(rls):
    """``set_grant`` through a connection bound the way the DAL binds it."""
    from robothor.plugins.grants import clear_grant, set_grant

    db, as_tenant = rls
    bound: list[str] = []

    def platform_connect(tenant_id):
        bound.append(tenant_id)

        @contextlib.contextmanager
        def connect():
            conn = as_tenant(tenant_id)
            try:
                yield conn
            finally:
                conn.close()

        return connect

    from robothor.plugins import grants

    original = grants._platform_connect
    grants._platform_connect = platform_connect
    try:
        set_grant("acme-child-a", "acme-tools", "off", "operator:alice", root_tenant="acme-parent")
        assert _row(db) == "off"
        assert clear_grant("acme-child-a", "acme-tools", root_tenant="acme-parent") is True
    finally:
        grants._platform_connect = original
    assert bound == ["acme-parent", "acme-parent"]
