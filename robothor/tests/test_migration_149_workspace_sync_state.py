"""149: the ingest state tables, and PgIngestStore against them.

The unit suite proves the replay rules on :class:`MemoryIngestStore`; this
proves the real store keeps the same promises on PostgreSQL: a position
round-trips, a seen id is seen exactly once per tenant, purge is per tenant
and by age, and both tables carry the tenant policy.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "crm" / "migrations"
MIGRATION = "149_workspace_sync_state"


def test_migration_file_exists_and_is_in_the_manifest():
    assert (MIGRATIONS_DIR / f"{MIGRATION}.sql").exists()
    manifest = (Path(__file__).resolve().parents[1] / "migrations" / "manifest.txt").read_text()
    assert f"crm/{MIGRATION}.sql" in manifest.splitlines()


@pytest.fixture
def pg_store(scratch_db):
    import psycopg2

    from robothor.workspace.ingest.state import PgIngestStore

    db, dsn = scratch_db(through=MIGRATION)

    @contextlib.contextmanager
    def connect():
        conn = psycopg2.connect(dsn)
        try:
            yield conn
        finally:
            conn.close()

    return db, PgIngestStore(connect)


@pytest.mark.integration
def test_a_position_round_trips(pg_store):
    from robothor.workspace.ingest.state import SyncState

    _db, store = pg_store
    when = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    state = SyncState(
        "tenant-a",
        "microsoft365",
        "Assistant@Example.com",
        "mail",
        delta_link="https://graph.microsoft.com/v1.0/x?$deltatoken=1",
        high_water=when,
        initialized_at=when,
    )

    async def go():
        assert await store.load("tenant-a", "microsoft365", "assistant@example.com", "mail") is None
        await store.save(state)
        await store.save(state.with_(high_water=when + timedelta(minutes=1)))
        return await store.load("tenant-a", "microsoft365", "assistant@example.com", "mail")

    loaded = asyncio.run(go())
    assert loaded is not None
    assert loaded.delta_link == state.delta_link
    assert loaded.high_water == when + timedelta(minutes=1)
    assert loaded.mailbox == "assistant@example.com"
    assert loaded.updated_at is not None


@pytest.mark.integration
def test_seen_is_per_tenant_idempotent_and_purged_by_age(pg_store):
    db, store = pg_store

    async def go():
        await store.mark_seen("tenant-a", "microsoft365", ["mail:1", "mail:2"])
        await store.mark_seen("tenant-a", "microsoft365", ["mail:2"])  # no conflict error
        await store.mark_seen("tenant-b", "microsoft365", ["mail:3"])
        a = await store.seen("tenant-a", "microsoft365", ["mail:1", "mail:2", "mail:3"])
        b = await store.seen("tenant-b", "microsoft365", ["mail:1", "mail:3"])
        return a, b

    a, b = asyncio.run(go())
    assert a == {"mail:1", "mail:2"}
    assert b == {"mail:3"}

    with db.cursor() as cur:
        cur.execute(
            "UPDATE workspace_seen SET seen_at = now() - interval '60 days' "
            "WHERE external_id IN ('mail:1', 'mail:3')"
        )
    cutoff = datetime.now(UTC) - timedelta(days=45)
    purged = asyncio.run(store.purge_seen("tenant-a", cutoff))
    assert purged == 1
    left = asyncio.run(store.seen("tenant-a", "microsoft365", ["mail:1", "mail:2"]))
    assert left == {"mail:2"}
    # tenant-b's old row is tenant-b's to purge.
    assert asyncio.run(store.seen("tenant-b", "microsoft365", ["mail:3"])) == {"mail:3"}


@pytest.mark.integration
def test_both_tables_are_tenant_isolated(pg_store):
    db, _store = pg_store
    with db.cursor() as cur:
        cur.execute(
            "SELECT tablename FROM pg_policies WHERE policyname = 'tenant_isolation' "
            "AND tablename IN ('workspace_sync_state', 'workspace_seen') ORDER BY tablename"
        )
        assert [r[0] for r in cur.fetchall()] == ["workspace_seen", "workspace_sync_state"]
        cur.execute(
            "SELECT relname FROM pg_class WHERE relforcerowsecurity "
            "AND relname IN ('workspace_sync_state', 'workspace_seen') ORDER BY relname"
        )
        assert [r[0] for r in cur.fetchall()] == ["workspace_seen", "workspace_sync_state"]


@pytest.mark.integration
def test_migration_is_idempotent(pg_store):
    db, _store = pg_store
    from robothor.db.migrate import _strip_outer_transaction

    sql = (MIGRATIONS_DIR / f"{MIGRATION}.sql").read_text()
    with db.cursor() as cur:
        cur.execute(_strip_outer_transaction(sql))
        cur.execute("SELECT COUNT(*) FROM workspace_sync_state")
        assert cur.fetchone()[0] == 0
