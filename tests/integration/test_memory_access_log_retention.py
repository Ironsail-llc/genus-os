"""Access-log retention and roll-up — real DB, no mocks.

``fact_access_log`` is the only usefulness signal the memory system has: it
records which facts a run actually consulted. ``cleanup_old_access_logs``
hard-DELETEs rows past a retention window, and the nightly maintenance pass
called it with a hardcoded 30 days. That destroyed the exact data the decay
formula needs to stop guessing (``access_count`` is non-zero on 21 of ~153k
rows precisely because the signal was being thrown away).

Two guarantees are pinned here:

1. Retention is configurable, so the window can be widened without a deploy.
2. Deletion is lossless in aggregate — every row GC removes is first folded
   into ``fact_access_rollup`` in the *same transaction*, so a fact's lifetime
   access count survives even after its raw log rows age out.

These run against real Postgres because the guarantee is transactional. A
mocked cursor would happily report a roll-up that never committed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

MIGRATIONS = Path(__file__).resolve().parents[2] / "crm" / "migrations"


def _apply(cur, filename: str) -> None:
    """Apply a migration file inside the caller's open transaction.

    The runner strips a file's own BEGIN/COMMIT so it can own the transaction
    (``robothor/db/migrate.py``). We do the same, otherwise a COMMIT inside the
    file would defeat the fixture's per-test rollback and leak into the DB.
    """
    from robothor.db.migrate import _strip_outer_transaction

    cur.execute(_strip_outer_transaction((MIGRATIONS / filename).read_text()))


@pytest.fixture
def access_log_schema(db_conn, db_cursor, test_prefix):
    """Ensure fact_access_log + fact_access_rollup exist, with a seed tenant.

    ``robothor_test`` has memory_facts but not fact_access_log — migration 043
    was never applied there. Applying both migrations in-transaction also means
    this test doubles as a check that they are idempotent and self-consistent.
    """
    _apply(db_cursor, "043_fact_outcomes.sql")
    _apply(db_cursor, "092_memory_access_rollup.sql")

    tenant = f"{test_prefix}-tenant"
    db_cursor.execute(
        "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
        (tenant, tenant),
    )
    db_cursor.execute(
        "INSERT INTO memory_facts (fact_text, category, tenant_id) "
        "VALUES (%s, 'personal', %s) RETURNING id",
        (f"{test_prefix} the operator prefers tabs", tenant),
    )
    fact_id = db_cursor.fetchone()["id"]
    return {"tenant": tenant, "fact_id": fact_id}


def _log_access(cur, *, tenant: str, fact_id: int, run: str, age_days: int) -> None:
    cur.execute(
        "INSERT INTO fact_access_log (run_id, tenant_id, fact_id, accessed_at) "
        "VALUES (%s, %s, %s, NOW() - make_interval(days => %s))",
        (run, tenant, fact_id, age_days),
    )


def test_retention_window_is_configurable(monkeypatch, access_log_schema, mock_get_connection, db_cursor):
    """A 400-day window must retain a 90-day-old row that the 30-day default drops.

    This is the knob that stops the bleed while the durable roll-up lands.
    """
    from robothor.memory import outcomes

    tenant, fact_id = access_log_schema["tenant"], access_log_schema["fact_id"]
    _log_access(db_cursor, tenant=tenant, fact_id=fact_id, run="r-old", age_days=90)

    monkeypatch.setenv("MEMORY_ACCESS_LOG_RETENTION_DAYS", "400")
    outcomes.cleanup_old_access_logs(tenant_id=tenant)

    db_cursor.execute("SELECT count(*) AS n FROM fact_access_log WHERE tenant_id = %s", (tenant,))
    assert db_cursor.fetchone()["n"] == 1, "90-day-old row deleted despite a 400-day retention window"


def test_gc_rolls_up_before_deleting(access_log_schema, mock_get_connection, db_cursor):
    """Rows GC removes must survive as an aggregate, in the same transaction.

    Three accesses age out and one stays. After GC the raw log keeps only the
    recent row, but the rollup must account for all four — otherwise the decay
    formula's backfill source silently shrinks every night.
    """
    from robothor.memory import outcomes

    tenant, fact_id = access_log_schema["tenant"], access_log_schema["fact_id"]
    for i, age in enumerate((95, 80, 65)):
        _log_access(db_cursor, tenant=tenant, fact_id=fact_id, run=f"r-old-{i}", age_days=age)
    _log_access(db_cursor, tenant=tenant, fact_id=fact_id, run="r-recent", age_days=1)

    deleted = outcomes.cleanup_old_access_logs(days=30, tenant_id=tenant)
    assert deleted == 3

    db_cursor.execute("SELECT count(*) AS n FROM fact_access_log WHERE tenant_id = %s", (tenant,))
    assert db_cursor.fetchone()["n"] == 1, "GC should leave only the in-window row"

    db_cursor.execute(
        "SELECT access_count, first_accessed_at, last_accessed_at FROM fact_access_rollup "
        "WHERE fact_id = %s AND tenant_id = %s",
        (fact_id, tenant),
    )
    row = db_cursor.fetchone()
    assert row is not None, "GC deleted rows without rolling them up — the signal is gone"
    assert row["access_count"] == 3, f"rollup lost accesses: {row['access_count']} != 3"
    assert row["first_accessed_at"] < row["last_accessed_at"]


def test_rollup_accumulates_across_successive_gc_passes(
    access_log_schema, mock_get_connection, db_cursor
):
    """A second GC pass must add to the rollup, not overwrite it.

    This is the regression that would quietly reintroduce the data loss: an
    INSERT that clobbers on conflict looks correct on day one and loses every
    prior night's history on day two.
    """
    from robothor.memory import outcomes

    tenant, fact_id = access_log_schema["tenant"], access_log_schema["fact_id"]

    _log_access(db_cursor, tenant=tenant, fact_id=fact_id, run="night-1", age_days=95)
    outcomes.cleanup_old_access_logs(days=30, tenant_id=tenant)

    _log_access(db_cursor, tenant=tenant, fact_id=fact_id, run="night-2", age_days=90)
    outcomes.cleanup_old_access_logs(days=30, tenant_id=tenant)

    db_cursor.execute(
        "SELECT access_count FROM fact_access_rollup WHERE fact_id = %s AND tenant_id = %s",
        (fact_id, tenant),
    )
    assert db_cursor.fetchone()["access_count"] == 2, "second GC pass overwrote the first"


def test_gc_is_tenant_scoped(access_log_schema, mock_get_connection, db_cursor, test_prefix):
    """A tenant-scoped sweep must not touch another tenant's log or rollup."""
    from robothor.memory import outcomes

    tenant, fact_id = access_log_schema["tenant"], access_log_schema["fact_id"]
    other = f"{test_prefix}-other"
    db_cursor.execute(
        "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
        (other, other),
    )
    _log_access(db_cursor, tenant=tenant, fact_id=fact_id, run="mine", age_days=95)
    _log_access(db_cursor, tenant=other, fact_id=fact_id, run="theirs", age_days=95)

    outcomes.cleanup_old_access_logs(days=30, tenant_id=tenant)

    db_cursor.execute("SELECT count(*) AS n FROM fact_access_log WHERE tenant_id = %s", (other,))
    assert db_cursor.fetchone()["n"] == 1, "tenant-scoped GC deleted another tenant's rows"
    db_cursor.execute("SELECT count(*) AS n FROM fact_access_rollup WHERE tenant_id = %s", (other,))
    assert db_cursor.fetchone()["n"] == 0, "tenant-scoped GC rolled up another tenant's rows"


def test_nightly_maintenance_uses_configured_retention(monkeypatch):
    """The nightly caller must honour the knob, not a hardcoded literal.

    Pinning this at the call site matters: parameterizing the function while
    leaving ``cleanup_old_access_logs(30, tid)`` in lifecycle.py would make the
    flag look present and do nothing — the exact failure mode this codebase has
    a standing lesson about.
    """
    import inspect

    from robothor.memory import lifecycle

    src = inspect.getsource(lifecycle)
    assert "cleanup_old_access_logs, 30," not in src, (
        "nightly maintenance still passes a hardcoded 30-day retention; "
        "it must use the configured window"
    )


def test_retention_default_is_unchanged_when_env_absent(monkeypatch):
    """Absent the env var, behaviour is the historical 30 days."""
    from robothor.memory import outcomes

    monkeypatch.delenv("MEMORY_ACCESS_LOG_RETENTION_DAYS", raising=False)
    assert outcomes.access_log_retention_days() == 30


@pytest.mark.parametrize("raw,expected", [("400", 400), ("", 30), ("nonsense", 30), ("0", 30)])
def test_retention_env_parsing_is_defensive(monkeypatch, raw, expected):
    """A malformed retention value must fall back to the default, never to 0.

    A 0-day window would delete the entire log on the next nightly pass, so
    parsing failures must fail safe rather than fail destructive.
    """
    from robothor.memory import outcomes

    monkeypatch.setenv("MEMORY_ACCESS_LOG_RETENTION_DAYS", raw)
    assert outcomes.access_log_retention_days() == expected
