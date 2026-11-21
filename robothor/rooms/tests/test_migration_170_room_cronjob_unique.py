"""170: one schedule row per room in ``user_cronjobs``.

A partial unique index on ``(tenant_id, room_id) WHERE room_id IS NOT NULL``,
so the room schedule sync's ``INSERT ... ON CONFLICT`` cannot leave two rows
for one room. Plain user crons (no ``room_id``) are untouched. Applied to a
scratch database where it lands in the chain, then applied again.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
MIGRATION = "170_room_cronjob_unique"
SQL_FILE = REPO / "crm" / "migrations" / f"{MIGRATION}.sql"
MANIFEST = REPO / "robothor" / "migrations" / "manifest.txt"

_INSERT = (
    "INSERT INTO user_cronjobs (job_id, tenant_id, agent_id, schedule_kind, schedule_payload, "
    "prompt, room_id, created_at) VALUES (%s, 'default', 'main', 'interval', '{}', '', %s, %s)"
)


def test_the_migration_follows_169_in_the_manifest() -> None:
    entries = [line.strip() for line in MANIFEST.read_text().splitlines()]
    assert f"crm/{MIGRATION}.sql" in entries
    assert entries.index(f"crm/{MIGRATION}.sql") > entries.index("crm/169_rooms.sql")


@pytest.mark.integration
def test_it_applies_twice_and_refuses_a_second_row_per_room(scratch_db) -> None:
    import psycopg2.errors

    db, _ = scratch_db(through=MIGRATION)
    room = str(uuid.uuid4())
    with db.cursor() as cur:
        cur.execute(SQL_FILE.read_text())
        cur.execute(SQL_FILE.read_text())
        cur.execute(_INSERT, ("ucron-a", room, "2026-11-21"))
        # Plain user crons carry no room and may be as many as they like.
        cur.execute(_INSERT, ("ucron-b", None, "2026-11-21"))
        cur.execute(_INSERT, ("ucron-c", None, "2026-11-21"))
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute(_INSERT, ("ucron-d", room, "2026-11-21"))


@pytest.mark.integration
def test_it_keeps_the_oldest_row_when_a_room_already_has_two(scratch_db) -> None:
    db, _ = scratch_db(through="169_rooms")
    room = str(uuid.uuid4())
    with db.cursor() as cur:
        cur.execute(_INSERT, ("ucron-new", room, "2026-11-21"))
        cur.execute(_INSERT, ("ucron-old", room, "2026-11-20"))
        cur.execute(SQL_FILE.read_text())
        cur.execute("SELECT job_id FROM user_cronjobs WHERE room_id = %s", (room,))
        assert [r[0] for r in cur.fetchall()] == ["ucron-old"]
