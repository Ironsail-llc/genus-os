"""156: the n8n snapshot tables, and PgN8nStore against them.

The unit suite proves the snapshot rules on :class:`MemoryN8nStore`; this
proves the real store keeps the same promises on PostgreSQL: workflows are
replaced by the latest listing, an execution is new exactly once per tenant,
the failure counts and sync state round-trip, purge is per tenant and by age,
and all three tables carry the tenant policy.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "crm" / "migrations"
MIGRATION = "156_n8n_integration"
TABLES = ["n8n_executions", "n8n_sync_state", "n8n_workflows"]


def test_migration_file_exists_and_is_in_the_manifest():
    assert (MIGRATIONS_DIR / f"{MIGRATION}.sql").exists()
    manifest = (Path(__file__).resolve().parents[1] / "migrations" / "manifest.txt").read_text()
    assert f"crm/{MIGRATION}.sql" in manifest.splitlines()


@pytest.fixture
def pg_store(scratch_db):
    import psycopg2

    from robothor.integrations.n8n.store import PgN8nStore

    db, dsn = scratch_db(through=MIGRATION)

    @contextlib.contextmanager
    def connect():
        conn = psycopg2.connect(dsn)
        try:
            yield conn
        finally:
            conn.close()

    return db, PgN8nStore(connect)


def _ex(eid: str, wid: str, at: datetime) -> dict:
    return {
        "execution_id": eid,
        "workflow_id": wid,
        "status": "error",
        "mode": "trigger",
        "started_at": at,
        "stopped_at": at,
    }


@pytest.mark.integration
def test_the_snapshot_round_trips(pg_store):
    _db, store = pg_store
    now = datetime.now(UTC)

    async def go():
        await store.upsert_workflows(
            "tenant-a",
            [
                {
                    "workflow_id": "wf1",
                    "name": "Invoice sync",
                    "active": True,
                    "tags": ["billing"],
                    "remote_updated_at": now,
                },
                {
                    "workflow_id": "wf2",
                    "name": "Gone soon",
                    "active": False,
                    "tags": [],
                    "remote_updated_at": None,
                },
            ],
        )
        await store.upsert_workflows(
            "tenant-a",
            [
                {
                    "workflow_id": "wf1",
                    "name": "Invoice sync",
                    "active": True,
                    "tags": ["billing"],
                    "remote_updated_at": now,
                }
            ],
        )
        first = await store.record_executions(
            "tenant-a",
            [_ex("1", "wf1", now - timedelta(hours=1)), _ex("2", "wf1", now - timedelta(days=3))],
        )
        again = await store.record_executions("tenant-a", [_ex("1", "wf1", now)])
        other = await store.record_executions("tenant-b", [_ex("1", "wf1", now)])
        await store.set_error_messages("tenant-a", {"1": "boom"})
        pending = await store.pending_alerts("tenant-a")
        await store.mark_alerted("tenant-a", ["1", "2"])
        after = await store.pending_alerts("tenant-a")
        workflows = await store.list_workflows("tenant-a", now=now)
        executions = await store.list_executions("tenant-a", "wf1", 20)
        recent = await store.list_executions("tenant-a", None, 20, since=now - timedelta(days=1))
        await store.set_sync_state(
            "tenant-a", polled_at=now, ok=True, workflows_total=1, workflows_active=1
        )
        await store.set_sync_state("tenant-a", polled_at=now, ok=False, error="network")
        state = await store.get_sync_state("tenant-a")
        return first, again, other, pending, after, workflows, executions, recent, state

    first, again, other, pending, after, workflows, executions, recent, state = asyncio.run(go())
    assert sorted(first) == ["1", "2"]
    assert again == []
    assert other == ["1"]
    assert {p["execution_id"] for p in pending} == {"1", "2"}
    assert after == []
    assert [w["workflow_id"] for w in workflows] == ["wf1"]
    wf1 = workflows[0]
    assert wf1["tags"] == ["billing"]
    assert (wf1["failures_24h"], wf1["failures_7d"]) == (1, 2)
    assert wf1["last_error"] == "boom"
    assert [e["execution_id"] for e in executions] == ["1", "2"]
    assert [e["execution_id"] for e in recent] == ["1"]
    assert state is not None
    assert state.last_ok_at is not None and state.last_error == "network"
    assert (state.workflows_total, state.workflows_active) == (1, 1)


@pytest.mark.integration
def test_purge_is_per_tenant_and_by_age(pg_store):
    _db, store = pg_store
    now = datetime.now(UTC)

    async def go():
        await store.record_executions(
            "tenant-a", [_ex("1", "wf1", now), _ex("2", "wf1", now - timedelta(days=40))]
        )
        await store.record_executions("tenant-b", [_ex("3", "wf1", now - timedelta(days=40))])
        purged = await store.purge("tenant-a", older_than_days=30)
        a = await store.list_executions("tenant-a", None, 20)
        b = await store.list_executions("tenant-b", None, 20)
        return purged, a, b

    purged, a, b = asyncio.run(go())
    assert purged == 1
    assert [e["execution_id"] for e in a] == ["1"]
    assert [e["execution_id"] for e in b] == ["3"]


@pytest.mark.integration
def test_all_tables_are_tenant_isolated(pg_store):
    db, _store = pg_store
    with db.cursor() as cur:
        cur.execute(
            "SELECT tablename FROM pg_policies WHERE policyname = 'tenant_isolation' "
            "AND tablename LIKE 'n8n\\_%%' ORDER BY tablename"
        )
        assert [r[0] for r in cur.fetchall()] == TABLES
        cur.execute(
            "SELECT relname FROM pg_class WHERE relforcerowsecurity "
            "AND relname LIKE 'n8n\\_%%' AND relkind = 'r' ORDER BY relname"
        )
        assert [r[0] for r in cur.fetchall()] == TABLES


@pytest.mark.integration
def test_migration_is_idempotent(pg_store):
    db, _store = pg_store
    from robothor.db.migrate import _strip_outer_transaction

    sql = (MIGRATIONS_DIR / f"{MIGRATION}.sql").read_text()
    with db.cursor() as cur:
        cur.execute(_strip_outer_transaction(sql))
        cur.execute("SELECT COUNT(*) FROM n8n_workflows")
        assert cur.fetchone()[0] == 0
