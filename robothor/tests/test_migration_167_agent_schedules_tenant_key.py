"""``agent_schedules`` is keyed per tenant, not per agent.

The table carries the row-level security policy, and tenants share job ids
(two tenants can both run a ``concierge``). With ``agent_id`` as the whole key,
the first tenant to write a job id owned it: every other tenant's upsert hit
that row through ``ON CONFLICT`` and the policy refused the update.

The migration rewrites the key of a live table, so these run it against a real
scratch database at the point in the chain just before it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg2
import pytest

REPO = Path(__file__).resolve().parents[2]
MIGRATION = "167_agent_schedules_tenant_key"
PREVIOUS = "166_entity_recall_definer"
SQL_FILE = REPO / "crm" / "migrations" / f"{MIGRATION}.sql"
MANIFEST = REPO / "robothor" / "migrations" / "manifest.txt"
CHILD = "acme-child"
COLUMNS = (
    "agent_id, tenant_id, enabled, cron_expr, timezone, timeout_seconds, last_run_at, "
    "last_status, last_duration_ms, consecutive_errors, model_primary, delivery_mode, "
    "session_target, updated_at, created_at"
)


def test_the_migration_is_in_the_manifest() -> None:
    entries = [line.strip() for line in MANIFEST.read_text().splitlines()]
    assert f"crm/{MIGRATION}.sql" in entries


def _primary_key(cur: Any) -> list[str]:
    cur.execute(
        "SELECT a.attname FROM pg_index x "
        "CROSS JOIN LATERAL unnest(x.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord) "
        "JOIN pg_attribute a ON a.attrelid = x.indrelid AND a.attnum = k.attnum "
        "WHERE x.indrelid = 'agent_schedules'::regclass AND x.indisprimary ORDER BY k.ord"
    )
    return [row[0] for row in cur.fetchall()]


def _rows(cur: Any) -> list[tuple[Any, ...]]:
    cur.execute(f"SELECT {COLUMNS} FROM agent_schedules ORDER BY tenant_id, agent_id")  # noqa: S608
    return cur.fetchall()


def _seed(cur: Any) -> None:
    cur.execute("INSERT INTO crm_tenants (id, display_name) VALUES (%s, 'Child')", (CHILD,))
    cur.execute(
        "INSERT INTO agent_schedules (agent_id, tenant_id, cron_expr, last_status, "
        "consecutive_errors, model_primary, last_run_at) VALUES "
        "('alice-agent', 'default', '0 * * * *', 'completed', 2, 'model-a', NOW()), "
        "('alice-agent:heartbeat', 'default', '5 * * * *', 'failed', 0, NULL, NULL), "
        "('bob-agent', %s, '10 * * * *', NULL, 0, 'model-b', NULL)",
        (CHILD,),
    )


def _copy_row(cur: Any, agent_id: str) -> None:
    cur.execute(
        "INSERT INTO agent_schedules (agent_id, tenant_id, cron_expr) VALUES (%s, %s, '* * * * *')",
        (agent_id, CHILD),
    )


@pytest.mark.integration
def test_every_row_keeps_its_tenant_and_its_state(scratch_db) -> None:
    db, _ = scratch_db(through=PREVIOUS)
    cur = db.cursor()
    _seed(cur)
    before = _rows(cur)

    scratch_db(through=MIGRATION)

    assert _primary_key(cur) == ["tenant_id", "agent_id"]
    assert _rows(cur) == before


@pytest.mark.integration
def test_a_row_with_no_tenant_lands_in_the_column_default(scratch_db) -> None:
    db, _ = scratch_db(through=PREVIOUS)
    cur = db.cursor()
    cur.execute(
        "INSERT INTO agent_schedules (agent_id, tenant_id, cron_expr) "
        "VALUES ('alice-agent', NULL, '0 * * * *')"
    )

    scratch_db(through=MIGRATION)

    cur.execute("SELECT tenant_id FROM agent_schedules WHERE agent_id = 'alice-agent'")
    assert cur.fetchall() == [("default",)]
    cur.execute(
        "SELECT attnotnull FROM pg_attribute WHERE attrelid = 'agent_schedules'::regclass "
        "AND attname = 'tenant_id'"
    )
    assert cur.fetchone() == (True,)


@pytest.mark.integration
def test_the_key_is_per_tenant_and_still_unique_within_one(scratch_db) -> None:
    db, _ = scratch_db(through=PREVIOUS)
    cur = db.cursor()
    _seed(cur)
    scratch_db(through=MIGRATION)

    _copy_row(cur, "alice-agent")

    cur.execute("SELECT tenant_id FROM agent_schedules WHERE agent_id = 'alice-agent' ORDER BY 1")
    assert cur.fetchall() == [(CHILD,), ("default",)]
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _copy_row(cur, "alice-agent")


@pytest.mark.integration
def test_a_rerun_changes_nothing_even_once_tenants_share_a_job_id(scratch_db) -> None:
    """A renumber, or a ledger row removed by hand, must replay cleanly: rows two
    tenants legitimately share by then must not trip anything."""
    db, _ = scratch_db(through=PREVIOUS)
    cur = db.cursor()
    _seed(cur)
    scratch_db(through=MIGRATION)
    _copy_row(cur, "alice-agent")
    before = _rows(cur)

    cur.execute(SQL_FILE.read_text())

    assert _primary_key(cur) == ["tenant_id", "agent_id"]
    assert _rows(cur) == before


@pytest.mark.integration
def test_row_level_security_is_left_as_it_was(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    cur = db.cursor()
    cur.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE oid = 'agent_schedules'::regclass"
    )
    assert cur.fetchone() == (True, True)
    cur.execute("SELECT polname FROM pg_policy WHERE polrelid = 'agent_schedules'::regclass")
    assert cur.fetchall() == [("tenant_isolation",)]


@pytest.mark.integration
def test_nothing_references_the_old_key(scratch_db) -> None:
    """No foreign key points at ``agent_schedules``, composite or otherwise."""
    db, _ = scratch_db(through=MIGRATION)
    cur = db.cursor()
    cur.execute(
        "SELECT conname FROM pg_constraint WHERE confrelid = 'agent_schedules'::regclass"
    )
    assert cur.fetchall() == []
