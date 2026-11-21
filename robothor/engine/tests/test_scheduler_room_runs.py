"""The scheduler fires a workspace's schedule through ``robothor.rooms.runs``.

Story B7 (exactly one run per schedule) and A5 (a paused room's row is
disabled, so it is never due). A room's row is claimed before its run starts:
the claim moves ``next_run_at`` only when it still holds the value the tick
read, so a second tick or a second replica that read the same due row claims
nothing. The run then goes to ``execute_room_run`` (never the generic per-agent
dedup path, which would skip the room whenever its agent is busy elsewhere).

One engine serves one tenant (``EngineConfig.tenant_id``; see
docs/event-bus.md "One engine per tenant"), so the tick reads that tenant's
rows only, and a row of another tenant is never fired here.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from robothor.engine import user_cron
from robothor.rooms import runs
from robothor.rooms.tests.conftest import committed_connection, rooms_db  # noqa: F401

ROOM = str(uuid.uuid4())
DUE = datetime(2026, 11, 21, 7, 0, tzinfo=UTC)


def _row(**kw: Any) -> dict[str, Any]:
    row = {
        "job_id": "ucron-room1",
        "room_id": ROOM,
        "agent_id": "main",
        "prompt": "Summarise sales.",
        "schedule_payload": {"kind": "interval", "every_seconds": 3600},
        "next_run_at": DUE,
        "fire_count": 0,
        "max_fires": None,
    }
    row.update(kw)
    return row


def _scheduler(monkeypatch, rows: list[dict[str, Any]], *, claim: bool = True):
    import robothor.engine.scheduler as sched_mod

    s = sched_mod.CronScheduler.__new__(sched_mod.CronScheduler)
    s.config = SimpleNamespace(tenant_id="test-tenant")
    s.runner = SimpleNamespace(execute=AsyncMock())
    seen: dict[str, Any] = {"claims": [], "runs": [], "tenants": []}
    monkeypatch.setattr(user_cron, "list_due_cronjobs", lambda tenant, now: [])

    def due_rooms(tenant: str, now: datetime) -> list[dict[str, Any]]:
        seen["tenants"].append(tenant)
        return rows

    def fake_claim(job_id: str, *, due_at: Any, next_run_at: Any) -> bool:
        seen["claims"].append((job_id, due_at, next_run_at))
        return claim

    async def fake_run(tenant_id: str, room_id: str, **kw: Any) -> None:
        seen["runs"].append((tenant_id, room_id, kw))

    monkeypatch.setattr(user_cron, "list_due_room_cronjobs", due_rooms)
    monkeypatch.setattr(user_cron, "claim_room_cronjob", fake_claim)
    monkeypatch.setattr(runs, "execute_room_run", fake_run)
    return s, seen


async def test_a_due_room_is_claimed_then_run_through_the_room_path(monkeypatch):
    s, seen = _scheduler(monkeypatch, [_row()])

    await s._tick_user_cronjobs()
    await asyncio.sleep(0)

    ((job_id, due_at, next_run),) = seen["claims"]
    assert (job_id, due_at) == ("ucron-room1", DUE)
    assert next_run > DUE
    assert seen["runs"] == [("test-tenant", ROOM, {"trigger": "schedule"})]
    s.runner.execute.assert_not_called()  # never the plain cron prompt


async def test_an_unclaimed_row_does_not_run(monkeypatch):
    s, seen = _scheduler(monkeypatch, [_row()], claim=False)

    await s._tick_user_cronjobs()
    await asyncio.sleep(0)

    assert seen["runs"] == []


async def test_the_tick_reads_the_engines_own_tenant(monkeypatch):
    s, seen = _scheduler(monkeypatch, [])
    await s._tick_user_cronjobs()
    assert seen["tenants"] == ["test-tenant"]


async def test_a_broken_room_row_does_not_starve_the_next(monkeypatch):
    s, seen = _scheduler(
        monkeypatch,
        [_row(job_id="bad", schedule_payload={"kind": "cron"}), _row(job_id="good")],
    )

    await s._tick_user_cronjobs()
    await asyncio.sleep(0)

    assert [c[0] for c in seen["claims"]] == ["good"]
    assert len(seen["runs"]) == 1


async def test_the_tick_binds_its_runner_for_room_runs(monkeypatch):
    s, _ = _scheduler(monkeypatch, [])
    runs.bind_runner(None)
    await s._tick_user_cronjobs()
    try:
        assert runs.bound_runner() is s.runner
    finally:
        runs.bind_runner(None)


# ─── the claim, against Postgres ─────────────────────────────────────────


@pytest.fixture
def cron_db(rooms_db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(user_cron, "get_connection", committed_connection)
    yield rooms_db
    rooms_db.cur.execute(
        "DELETE FROM user_cronjobs WHERE tenant_id = ANY(%s)", ([rooms_db.tenant, rooms_db.other],)
    )


def _seed(db: Any, tenant: str, *, enabled: bool = True, due: bool = True) -> str:
    room_id = str(uuid.uuid4())
    user_cron.sync_room_cronjob(
        tenant_id=tenant,
        room_id=room_id,
        agent_id="main",
        prompt="Summarise sales.",
        schedule={"kind": "interval", "every_seconds": 3600},
        run_as_user_id=db.alice,
        enabled=enabled,
    )
    if due:
        db.cur.execute(
            "UPDATE user_cronjobs SET next_run_at = now() - interval '1 minute' "
            "WHERE tenant_id = %s AND room_id = %s",
            (tenant, room_id),
        )
    return room_id


@pytest.mark.integration
def test_due_room_rows_are_this_tenants_enabled_and_due_only(cron_db):
    db = cron_db
    mine = _seed(db, db.tenant)
    _seed(db, db.tenant, enabled=False)
    _seed(db, db.tenant, due=False)
    _seed(db, db.other)

    due = user_cron.list_due_room_cronjobs(db.tenant, datetime.now(UTC))

    assert [r["room_id"] for r in due] == [mine]
    assert user_cron.list_due_cronjobs(db.tenant, datetime.now(UTC)) == []


@pytest.mark.integration
def test_two_claims_of_one_due_row_have_one_winner(cron_db):
    db = cron_db
    _seed(db, db.tenant)
    (row,) = user_cron.list_due_room_cronjobs(db.tenant, datetime.now(UTC))
    nxt = datetime.now(UTC) + timedelta(hours=1)

    first = user_cron.claim_room_cronjob(row["job_id"], due_at=row["next_run_at"], next_run_at=nxt)
    second = user_cron.claim_room_cronjob(row["job_id"], due_at=row["next_run_at"], next_run_at=nxt)

    assert (first, second) == (True, False)
    assert user_cron.list_due_room_cronjobs(db.tenant, datetime.now(UTC)) == []
