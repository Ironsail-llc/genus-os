# ruff: noqa: F811 - pytest fixtures are imported and requested by name
"""A room's schedule row in ``user_cronjobs``.

A scheduled workspace keeps one row, keyed by ``room_id`` and carrying the
user it runs as. The row is synced on every schedule change, pause, resume,
archive and reassignment (``robothor.rooms.definition``). A cron schedule is
read in the room's time zone. The generic user-cron tick does not fire room
rows: a room's run is a run in that room as its run-as user, not a plain cron
prompt under the service identity.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from robothor.engine import user_cron
from robothor.rooms.tests.conftest import committed_connection, rooms_db  # noqa: F401


def test_a_cron_schedule_is_read_in_its_time_zone():
    after = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
    nxt = user_cron.compute_next_run(
        {"kind": "cron", "expression": "0 8 * * *", "tz": "America/New_York"}, after
    )
    assert nxt is not None
    local = nxt.astimezone(ZoneInfo("America/New_York"))
    assert (local.hour, local.minute) == (8, 0)


def test_a_plain_cron_job_without_a_time_zone_fires_exactly_as_before():
    """A ``register_user_cron`` row carries no ``tz``: it keeps APScheduler's
    default (the host's zone), so no existing job moves."""
    from robothor.engine.crontab import cron_trigger

    after = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
    existing = {"kind": "cron", "expression": "0 9 * * *"}

    expected = cron_trigger("0 9 * * *").get_next_fire_time(None, after)

    assert user_cron.compute_next_run(existing, after) == expected


def test_a_room_schedule_row_always_carries_its_time_zone():
    from robothor.rooms import definition

    assert definition._parse_schedule("0 8 * * *", None)["tz"] == "UTC"
    assert definition._parse_schedule("0 8 * * *", "Europe/London")["tz"] == "Europe/London"


@pytest.fixture
def cron_db(rooms_db, monkeypatch):
    monkeypatch.setattr(user_cron, "get_connection", committed_connection)
    yield rooms_db
    rooms_db.cur.execute(
        "DELETE FROM user_cronjobs WHERE tenant_id = ANY(%s)", ([rooms_db.tenant, rooms_db.other],)
    )


SCHEDULE = {"kind": "interval", "every_seconds": 3600}


@pytest.mark.integration
def test_sync_creates_then_updates_one_row_per_room(cron_db):
    db = cron_db
    room_id = str(uuid.uuid4())

    first = user_cron.sync_room_cronjob(
        tenant_id=db.tenant,
        room_id=room_id,
        agent_id="main",
        prompt="Summarise.",
        schedule=SCHEDULE,
        run_as_user_id=db.alice,
        enabled=True,
    )
    second = user_cron.sync_room_cronjob(
        tenant_id=db.tenant,
        room_id=room_id,
        agent_id="main",
        prompt="Summarise again.",
        schedule={"kind": "cron", "expression": "0 9 * * *", "tz": "UTC"},
        run_as_user_id=db.bob,
        enabled=True,
    )

    assert first is not None and second is not None
    assert first["job_id"] == second["job_id"]
    row = user_cron.get_room_cronjob(db.tenant, room_id)
    assert row is not None
    assert row["schedule_kind"] == "cron"
    assert row["run_as_user_id"] == db.bob
    assert row["prompt"] == "Summarise again."
    assert row["enabled"] is True
    assert row["next_run_at"].astimezone(UTC).hour == 9


@pytest.mark.integration
def test_disabling_or_removing_the_schedule_stops_the_row(cron_db):
    db = cron_db
    room_id = str(uuid.uuid4())
    kwargs = {
        "tenant_id": db.tenant,
        "room_id": room_id,
        "agent_id": "main",
        "prompt": "Summarise.",
        "run_as_user_id": db.alice,
    }
    user_cron.sync_room_cronjob(schedule=SCHEDULE, enabled=True, **kwargs)

    assert user_cron.sync_room_cronjob(schedule=SCHEDULE, enabled=False, **kwargs) is not None
    row = user_cron.get_room_cronjob(db.tenant, room_id)
    assert row["enabled"] is False and row["next_run_at"] is None

    before = datetime.now(UTC)
    user_cron.sync_room_cronjob(schedule=SCHEDULE, enabled=True, **kwargs)
    row = user_cron.get_room_cronjob(db.tenant, room_id)
    assert row["enabled"] is True
    assert before <= row["next_run_at"] <= before + timedelta(seconds=3600 + 60)

    assert user_cron.sync_room_cronjob(schedule=None, enabled=True, **kwargs) is None
    assert user_cron.get_room_cronjob(db.tenant, room_id)["enabled"] is False


@pytest.mark.integration
def test_removing_a_schedule_that_never_existed_writes_nothing(cron_db):
    db = cron_db
    room_id = str(uuid.uuid4())
    user_cron.sync_room_cronjob(
        tenant_id=db.tenant,
        room_id=room_id,
        agent_id="main",
        prompt="",
        schedule=None,
        run_as_user_id=db.alice,
        enabled=True,
    )
    assert user_cron.get_room_cronjob(db.tenant, room_id) is None


@pytest.mark.integration
def test_a_room_row_is_another_tenants_business(cron_db):
    db = cron_db
    room_id = str(uuid.uuid4())
    user_cron.sync_room_cronjob(
        tenant_id=db.tenant,
        room_id=room_id,
        agent_id="main",
        prompt="Summarise.",
        schedule=SCHEDULE,
        run_as_user_id=db.alice,
        enabled=True,
    )
    assert user_cron.get_room_cronjob(db.other, room_id) is None


@pytest.mark.integration
def test_the_generic_tick_does_not_fire_room_rows(cron_db):
    db = cron_db
    room_id = str(uuid.uuid4())
    user_cron.sync_room_cronjob(
        tenant_id=db.tenant,
        room_id=room_id,
        agent_id="main",
        prompt="Summarise.",
        schedule=SCHEDULE,
        run_as_user_id=db.alice,
        enabled=True,
    )
    plain = user_cron.create_user_cronjob(
        agent_id="main", prompt="Remind me.", schedule=SCHEDULE, tenant_id=db.tenant
    )

    due = user_cron.list_due_cronjobs(db.tenant, now=datetime.now(UTC) + timedelta(days=1))

    assert [r["job_id"] for r in due] == [plain["job_id"]]


@pytest.mark.integration
def test_concurrent_syncs_of_a_new_room_leave_one_row(cron_db):
    import threading

    db = cron_db
    room_id = str(uuid.uuid4())
    barrier = threading.Barrier(6)
    errors: list[BaseException] = []

    def sync() -> None:
        barrier.wait()
        try:
            user_cron.sync_room_cronjob(
                tenant_id=db.tenant,
                room_id=room_id,
                agent_id="main",
                prompt="Summarise.",
                schedule=SCHEDULE,
                run_as_user_id=db.alice,
                enabled=True,
            )
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=sync) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    db.cur.execute(
        "SELECT COUNT(*) FROM user_cronjobs WHERE tenant_id = %s AND room_id = %s",
        (db.tenant, room_id),
    )
    assert db.cur.fetchone()[0] == 1


@pytest.mark.integration
def test_the_database_refuses_a_second_row_for_one_room(cron_db):
    import psycopg2.errors

    db = cron_db
    room_id = str(uuid.uuid4())
    insert = (
        "INSERT INTO user_cronjobs (job_id, tenant_id, agent_id, schedule_kind, "
        "schedule_payload, prompt, room_id) VALUES (%s, %s, 'main', 'interval', '{}', '', %s)"
    )
    db.cur.execute(insert, (f"ucron-{uuid.uuid4().hex[:12]}", db.tenant, room_id))
    with pytest.raises(psycopg2.errors.UniqueViolation):
        db.cur.execute(insert, (f"ucron-{uuid.uuid4().hex[:12]}", db.tenant, room_id))
