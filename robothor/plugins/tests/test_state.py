"""``PluginState`` — the one table a plugin wheel stores its data in.

A plugin wheel cannot ship a migration, so every "table" a plugin needs is a
row *kind* in ``plugin_state`` (migration 154), keyed by
``(tenant_id, plugin, kind, key)``.

Two halves. The unit half pins the contract a plugin author programs against
without a database: bad arguments are refused before any SQL runs, and every
statement is scoped to the plugin and the tenant and hides expired rows. The
integration half proves the same promises on PostgreSQL: a value round-trips,
a TTL'd row disappears when it expires, purge removes it, two plugins (and two
tenants) never see each other's rows, and listing honours a key prefix.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "crm" / "migrations"
MANIFEST = Path(__file__).resolve().parents[2] / "migrations" / "manifest.txt"
MIGRATION = "154_plugin_state"


# ─── unit: the contract, no database ─────────────────────────────────────


def _recording_connect(fetchone: Any = None, fetchall: list[Any] | None = None, rowcount: int = 0):
    """A ``connect`` that hands out one mocked connection and records the SQL."""
    cur = MagicMock()
    cur.fetchone.return_value = fetchone
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


def _state(connect, plugin: str = "acme", tenant_id: str = "test-tenant"):
    from robothor.plugins.state import PluginState

    return PluginState(plugin, tenant_id=tenant_id, connect=connect)


def test_tenant_defaults_to_the_platform_default():
    from robothor.constants import DEFAULT_TENANT
    from robothor.plugins.state import PluginState

    assert PluginState("acme").tenant_id == DEFAULT_TENANT


@pytest.mark.parametrize("plugin", ["", "   "])
def test_a_plugin_needs_a_name(plugin):
    from robothor.plugins.state import PluginState

    with pytest.raises(ValueError):
        PluginState(plugin)


def test_put_refuses_a_value_that_is_not_a_dict():
    connect, cur = _recording_connect()
    with pytest.raises(TypeError):
        _state(connect).put("contact", "k1", ["not", "a", "dict"])  # type: ignore[arg-type]
    cur.execute.assert_not_called()


@pytest.mark.parametrize("ttl", [0, -5])
def test_put_refuses_a_ttl_that_is_not_positive(ttl):
    connect, cur = _recording_connect()
    with pytest.raises(ValueError):
        _state(connect).put("contact", "k1", {"a": 1}, ttl_seconds=ttl)
    cur.execute.assert_not_called()


def test_put_refuses_a_ttl_too_large_for_the_database():
    """A TTL past a 32-bit int would surface as a database error, not the promised ValueError."""
    connect, cur = _recording_connect()
    with pytest.raises(ValueError):
        _state(connect).put("contact", "k1", {"a": 1}, ttl_seconds=2**31)
    cur.execute.assert_not_called()


def test_put_accepts_the_largest_ttl():
    connect, cur = _recording_connect()
    _state(connect).put("contact", "k1", {"a": 1}, ttl_seconds=2**31 - 1)
    cur.execute.assert_called_once()


@pytest.mark.parametrize(("kind", "key"), [("", "k1"), ("contact", "")])
def test_kind_and_key_must_be_named(kind, key):
    connect, cur = _recording_connect()
    with pytest.raises(ValueError):
        _state(connect).get(kind, key)
    cur.execute.assert_not_called()


def test_list_refuses_a_limit_below_one():
    connect, cur = _recording_connect()
    with pytest.raises(ValueError):
        _state(connect).list("contact", limit=0)
    cur.execute.assert_not_called()


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.get("contact", "k1"),
        lambda s: s.exists("contact", "k1"),
        lambda s: s.list("contact"),
    ],
    ids=["get", "exists", "list"],
)
def test_reads_are_scoped_and_hide_expired_rows(call):
    connect, cur = _recording_connect()
    call(_state(connect, plugin="acme", tenant_id="test-tenant"))

    sql, params = cur.execute.call_args[0]
    assert "plugin_state" in sql
    assert "expires_at > now()" in sql
    assert "test-tenant" in params
    assert "acme" in params


def test_get_returns_none_for_a_missing_row():
    connect, _cur = _recording_connect(fetchone=None)
    assert _state(connect).get("contact", "k1") is None


def test_get_returns_the_stored_dict():
    connect, _cur = _recording_connect(fetchone=({"a": 1},))
    assert _state(connect).get("contact", "k1") == {"a": 1}


# ─── integration: the same promises on PostgreSQL ────────────────────────


def test_migration_file_exists_and_is_in_the_manifest():
    assert (MIGRATIONS_DIR / f"{MIGRATION}.sql").exists()
    assert f"crm/{MIGRATION}.sql" in MANIFEST.read_text().splitlines()


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

    def make(plugin: str = "acme", tenant_id: str = "test-tenant"):
        from robothor.plugins.state import PluginState

        return PluginState(plugin, tenant_id=tenant_id, connect=connect)

    return db, connect, make


def _expire(db, key: str) -> None:
    with db.cursor() as cur:
        cur.execute(
            "UPDATE plugin_state SET expires_at = now() - interval '1 second' WHERE key = %s",
            (key,),
        )


@pytest.mark.integration
def test_put_get_round_trip_and_overwrite(pg):
    _db, _connect, make = pg
    state = make()

    assert state.get("contact", "alice") is None
    assert state.exists("contact", "alice") is False

    state.put("contact", "alice", {"email": "agent@example.com", "n": 1})
    assert state.get("contact", "alice") == {"email": "agent@example.com", "n": 1}
    assert state.exists("contact", "alice") is True

    state.put("contact", "alice", {"n": 2})
    assert state.get("contact", "alice") == {"n": 2}


@pytest.mark.integration
def test_a_ttl_row_is_invisible_once_expired(pg):
    db, _connect, make = pg
    state = make()

    state.put("cache", "short", {"v": 1}, ttl_seconds=3600)
    state.put("cache", "forever", {"v": 2})
    assert state.get("cache", "short") == {"v": 1}
    [row] = [r for r in state.list("cache") if r["key"] == "short"]
    assert row["expires_at"] is not None

    _expire(db, "short")

    assert state.get("cache", "short") is None
    assert state.exists("cache", "short") is False
    assert [r["key"] for r in state.list("cache")] == ["forever"]


@pytest.mark.integration
def test_purge_removes_only_expired_rows(pg):
    db, _connect, make = pg
    state = make()
    state.put("cache", "old", {"v": 1}, ttl_seconds=60)
    state.put("cache", "live", {"v": 2}, ttl_seconds=3600)
    _expire(db, "old")

    assert state.purge_expired() == 1

    with db.cursor() as cur:
        cur.execute("SELECT key FROM plugin_state ORDER BY key")
        assert [r[0] for r in cur.fetchall()] == ["live"]


@pytest.mark.integration
def test_the_tenant_wide_purge_covers_every_plugin(pg):
    from robothor.plugins.state import purge_expired

    db, connect, make = pg
    make("acme").put("cache", "a-old", {"v": 1}, ttl_seconds=60)
    make("other").put("cache", "o-old", {"v": 1}, ttl_seconds=60)
    make("other", tenant_id="tenant-b").put("cache", "b-old", {"v": 1}, ttl_seconds=60)
    for key in ("a-old", "o-old", "b-old"):
        _expire(db, key)

    assert purge_expired("test-tenant", connect=connect) == 2

    with db.cursor() as cur:
        cur.execute("SELECT key FROM plugin_state")
        assert [r[0] for r in cur.fetchall()] == ["b-old"]


@pytest.mark.integration
def test_two_plugins_and_two_tenants_with_the_same_kind_and_key_are_distinct(pg):
    _db, _connect, make = pg
    make("acme").put("contact", "k", {"owner": "acme"})
    make("other").put("contact", "k", {"owner": "other"})
    make("acme", tenant_id="tenant-b").put("contact", "k", {"owner": "acme-b"})

    assert make("acme").get("contact", "k") == {"owner": "acme"}
    assert make("other").get("contact", "k") == {"owner": "other"}
    assert make("acme", tenant_id="tenant-b").get("contact", "k") == {"owner": "acme-b"}

    assert make("other").delete("contact", "k") is True
    assert make("other").delete("contact", "k") is False
    assert make("acme").get("contact", "k") == {"owner": "acme"}


@pytest.mark.integration
def test_list_filters_by_prefix_orders_by_key_and_limits(pg):
    _db, _connect, make = pg
    state = make()
    for key in ("lead:3", "lead:1", "lead:2", "deal:1", "lead_x"):
        state.put("pipeline", key, {"key": key})
    state.put("other-kind", "lead:9", {"key": "lead:9"})

    rows = state.list("pipeline", prefix="lead:")
    assert [r["key"] for r in rows] == ["lead:1", "lead:2", "lead:3"]
    assert rows[0]["value"] == {"key": "lead:1"}
    assert set(rows[0]) == {"key", "value", "updated_at", "expires_at"}

    # A LIKE wildcard in the prefix is a literal character, not a pattern.
    assert [r["key"] for r in state.list("pipeline", prefix="lead_")] == ["lead_x"]
    assert [r["key"] for r in state.list("pipeline", limit=2)] == ["deal:1", "lead:1"]


@pytest.mark.integration
def test_the_table_is_tenant_isolated(pg):
    db, _connect, _make = pg
    with db.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_policies WHERE policyname = 'tenant_isolation' "
            "AND tablename = 'plugin_state'"
        )
        assert cur.fetchone() is not None
        cur.execute("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'plugin_state'")
        assert cur.fetchone()[0] is True
