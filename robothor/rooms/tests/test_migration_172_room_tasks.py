"""172: workspace tasks and member approval.

- ``room_member_removals``: who was removed from a workspace, so opening a task
  never re-adds them (only someone who can share the workspace can).
- One suggestion key per workspace, enforced by the database as well as by the
  code that turns suggestions into tasks.
- An index for listing a member's own proposed actions.

Applied to a scratch database at the point in the chain where it lands, then
applied again: every statement must be idempotent.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
MIGRATION = "172_room_tasks_approval"
SQL_FILE = REPO / "crm" / "migrations" / f"{MIGRATION}.sql"
MANIFEST = REPO / "robothor" / "migrations" / "manifest.txt"


def test_the_migration_is_in_the_manifest() -> None:
    entries = [line.strip() for line in MANIFEST.read_text().splitlines()]
    assert f"crm/{MIGRATION}.sql" in entries


def _columns(cur: Any, table: str) -> set[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,)
    )
    return {r[0] for r in cur.fetchall()}


@pytest.mark.integration
def test_it_applies_twice_cleanly_with_forced_row_security(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    with db.cursor() as cur:
        cur.execute(SQL_FILE.read_text())
        cur.execute(SQL_FILE.read_text())
        cur.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = %s::regclass",
            ("room_member_removals",),
        )
        assert cur.fetchone() == (True, True)
        assert {
            "tenant_id", "room_id", "user_id", "removed_by", "removed_at", "created_at",
            "updated_at",
        } <= _columns(cur, "room_member_removals")  # fmt: skip


@pytest.mark.integration
def test_a_suggestion_key_is_unique_per_workspace(scratch_db) -> None:
    import psycopg2

    db, _ = scratch_db(through=MIGRATION)
    room = str(uuid.uuid4())
    with db.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants (id, display_name) VALUES ('t-172', 'T')")
        cur.execute(
            "INSERT INTO rooms (tenant_id, id, kind, title, agent_id, session_key, created_by) "
            "VALUES ('t-172', %s, 'report', 'R', 'main', %s, %s)",
            (room, f"agent:main:room:{room}", str(uuid.uuid4())),
        )
        insert = (
            "INSERT INTO room_tasks (tenant_id, room_id, task_id, origin, suggestion_key) "
            "VALUES ('t-172', %s, %s, 'suggestion', 'call-back')"
        )
        cur.execute(insert, (room, str(uuid.uuid4())))
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute(insert, (room, str(uuid.uuid4())))
