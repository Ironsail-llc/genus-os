"""Asking a report room for a period (story C3), against real Postgres.

A person picks a period. If its edition is published they get it at once. If
not, exactly one run starts for it, however many people asked at the same
moment, and everyone who asked gets the same building edition. On-demand runs
are capped per room per day; a period that is not over yet, a room that is not
a report, and a person who may only read are all refused without starting
anything. A week or a month run is handed the summaries of the day editions
inside it as data: never a sum of their figures.

The room run itself (``robothor.rooms.runs.run_room``) is injected; these tests
pass a fake and count its calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from robothor.rooms import dal, periods
from robothor.rooms.access import Actor
from robothor.rooms.errors import RoomForbidden, RoomNotFound
from robothor.rooms.periods import (
    CONTEXT_MAX_CHARS,
    CONTEXT_SUMMARY_CHARS,
    DEFAULT_MAX_ON_DEMAND_PER_DAY,
    OnDemandCapReached,
    Period,
    PeriodError,
    RoomRunUnavailable,
    context_for_period,
    last_complete_period,
    request_period,
)
from robothor.rooms.recovery import STALE_BUILDING_SECONDS, reap_stale_editions

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration


# ─── a database of this branch's own schema ─────────────────────────────


@pytest.fixture(scope="module")
def _scratch_dsn() -> Iterator[str]:
    """A throwaway database migrated through this branch's whole chain.

    The shared ``*_test`` database is migrated by whichever branch ran last, so
    its room tables need not be this branch's. The scratch database is.
    """
    import getpass
    import uuid

    psycopg2 = pytest.importorskip("psycopg2")
    from robothor.db.migrate import _discover, _strip_outer_transaction

    admin = os.environ.get("ROBOTHOR_TEST_ADMIN_DSN", f"dbname=postgres user={getpass.getuser()}")
    name = f"scratch_periods_{uuid.uuid4().hex[:8]}"
    try:
        root = psycopg2.connect(admin)
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"no admin connection at ROBOTHOR_TEST_ADMIN_DSN: {exc}")
    root.autocommit = True
    with root.cursor() as cur:
        cur.execute(f"CREATE DATABASE {name}")
    parts = dict(psycopg2.extensions.parse_dsn(admin))
    parts["dbname"] = name
    dsn = " ".join(f"{k}={v}" for k, v in parts.items())
    try:
        db = psycopg2.connect(dsn)
        db.autocommit = True
        with db.cursor() as cur:
            for ext in ("vector", "pgcrypto", '"uuid-ossp"'):
                with contextlib.suppress(Exception):
                    cur.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
            for migration in _discover():
                cur.execute(_strip_outer_transaction(migration.path.read_text()))
        db.close()
        yield dsn
    finally:
        with root.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        root.close()


@pytest.fixture
def rooms_db(_scratch_dsn, monkeypatch) -> Iterator[SimpleNamespace]:
    """The rooms conftest's fixture, on the scratch database."""
    import psycopg2

    @contextlib.contextmanager
    def connection(autocommit: bool = False, read_only: bool = False) -> Iterator[Any]:
        conn = psycopg2.connect(_scratch_dsn)
        conn.autocommit = autocommit or read_only
        try:
            yield conn
            if not conn.autocommit:
                conn.commit()
        except BaseException:
            if not conn.closed:
                conn.rollback()
            raise
        finally:
            conn.close()

    monkeypatch.setattr(dal, "get_connection", connection)
    conn = psycopg2.connect(_scratch_dsn)
    conn.autocommit = True
    cur = conn.cursor()
    tenant, other = "test-tenant", "test-tenant-other"
    for tid in (tenant, other):
        cur.execute(
            "INSERT INTO crm_tenants (id, display_name) VALUES (%s, 'Test tenant') "
            "ON CONFLICT DO NOTHING",
            (tid,),
        )
    try:
        yield SimpleNamespace(
            tenant=tenant,
            other=other,
            alice=str(uuid.uuid4()),
            bob=str(uuid.uuid4()),
            carol=str(uuid.uuid4()),
            cur=cur,
        )
    finally:
        # The database is this module's alone: clear the rooms between tests.
        cur.execute(
            "TRUNCATE room_tasks, room_editions, room_revisions, room_members, rooms CASCADE"
        )
        conn.close()


BLOCKS = [{"component": "render_markdown", "props": {"content": "All good."}}]
TODAY = datetime.now(UTC).date()


def _ago(days: int) -> str:
    return (TODAY - timedelta(days=days)).isoformat()


class FakeRun:
    """Stands in for ``robothor.rooms.runs.run_room``."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    async def __call__(self, room: Any, edition: Any, *, period: Period, context: str) -> None:
        await asyncio.sleep(0.05)  # long enough for racing requesters to overlap
        self.calls.append({"room": room, "edition": edition, "period": period, "context": context})
        if self.error is not None:
            raise self.error


async def _settle() -> None:
    await asyncio.gather(*periods.pending_runs(), return_exceptions=True)


def _report(db: Any, **settings: Any) -> Any:
    return dal.create_room(
        db.tenant,
        kind="report",
        title="Daily numbers",
        agent_id="main",
        created_by=db.alice,
        settings={"tz": "UTC", "default_grain": "day", **settings},
    )


def _alice(db: Any, role: str = "member") -> Actor:
    return Actor(user_id=db.alice, role=role, tenant_id=db.tenant)


def _published(db: Any, room: Any, grain: str, start: date, end: date, summary: str, **kw: Any):
    edition, _ = dal.create_edition(
        db.tenant, room.id, grain=grain, period_start=start, period_end=end, **kw
    )
    return dal.publish_edition(
        db.tenant, edition.id, title=f"{grain} {start}", summary=summary, blocks=BLOCKS
    )


def _editions(db: Any, room: Any) -> list[tuple[Any, ...]]:
    db.cur.execute(
        "SELECT grain, period_start, period_end, status FROM room_editions "
        "WHERE tenant_id = %s AND room_id = %s ORDER BY seq",
        (db.tenant, room.id),
    )
    return db.cur.fetchall()


# ─── an edition that exists ─────────────────────────────────────────────


async def test_a_published_edition_is_returned_at_once_without_a_run(rooms_db):
    room = _report(rooms_db)
    day = TODAY - timedelta(days=3)
    published = _published(rooms_db, room, "day", day, day, "Quiet day.")
    run = FakeRun()

    edition, started = await request_period(
        _alice(rooms_db), room.id, "day", day.isoformat(), run_room=run
    )
    await _settle()

    assert (edition.id, started) == (published.id, False)
    assert edition.status == "published"
    assert run.calls == []


async def test_a_period_already_building_is_shared_not_started_again(rooms_db):
    room = _report(rooms_db)
    run = FakeRun()
    first, started = await request_period(_alice(rooms_db), room.id, "day", _ago(2), run_room=run)
    await _settle()  # the fake leaves the edition building

    again, started_again = await request_period(
        _alice(rooms_db), room.id, "day", _ago(2), run_room=run
    )
    await _settle()

    assert started and not started_again
    assert again.id == first.id
    assert len(run.calls) == 1


# ─── a missing edition starts exactly one run ───────────────────────────


async def test_concurrent_requests_for_a_missing_period_start_exactly_one_run(rooms_db):
    room = _report(rooms_db)
    dal.add_member(rooms_db.tenant, room.id, rooms_db.bob, added_by=rooms_db.alice)
    bob = Actor(user_id=rooms_db.bob, role="member", tenant_id=rooms_db.tenant)
    run = FakeRun()
    week = last_complete_period("week", "UTC", datetime.now(UTC))

    results = await asyncio.gather(
        *(
            request_period(actor, room.id, "week", week.start.isoformat(), run_room=run)
            for actor in [_alice(rooms_db), bob] * 4
        )
    )
    await _settle()

    assert len({edition.id for edition, _ in results}) == 1
    assert sum(started for _, started in results) == 1
    assert len(run.calls) == 1
    assert run.calls[0]["period"] == week
    assert run.calls[0]["edition"].id == results[0][0].id
    assert _editions(rooms_db, room) == [("week", week.start, week.end, "building")]


async def test_the_started_edition_names_the_period_and_the_requester(rooms_db):
    room = _report(rooms_db, tz="Pacific/Auckland")
    run = FakeRun()

    edition, started = await request_period(_alice(rooms_db), room.id, "day", None, run_room=run)
    await _settle()

    expected = last_complete_period("day", "Pacific/Auckland", datetime.now(UTC))
    assert started
    assert (edition.grain, edition.period_start, edition.period_end) == (
        "day",
        expected.start,
        expected.end,
    )
    assert edition.requested_by == rooms_db.alice
    assert not edition.preview
    assert run.calls[0]["room"].id == room.id
    assert run.calls[0]["context"] == ""  # a day has no day editions inside it


async def test_a_run_that_fails_marks_its_edition_failed(rooms_db):
    room = _report(rooms_db)
    run = FakeRun(error=RuntimeError("the model is down"))

    edition, started = await request_period(_alice(rooms_db), room.id, "day", _ago(1), run_room=run)
    await _settle()

    stored = dal.get_edition(rooms_db.tenant, edition.id, viewer_id=rooms_db.alice)
    assert started
    assert stored.status == "failed"
    assert "the model is down" in stored.error


async def test_no_room_runner_refuses_before_anything_is_written(rooms_db, monkeypatch):
    room = _report(rooms_db)

    def unavailable() -> Any:
        raise RoomRunUnavailable("room runs are not available on this engine")

    monkeypatch.setattr(periods, "resolve_run_room", unavailable)

    with pytest.raises(RoomRunUnavailable):
        await request_period(_alice(rooms_db), room.id, "day", _ago(1))

    assert _editions(rooms_db, room) == []


# ─── the daily cap ──────────────────────────────────────────────────────


async def test_the_daily_cap_stops_new_runs_and_starts_nothing(rooms_db):
    room = _report(rooms_db, max_on_demand_per_day=2)
    run = FakeRun()
    for days in (1, 2):
        _, started = await request_period(
            _alice(rooms_db), room.id, "day", _ago(days), run_room=run
        )
        assert started

    with pytest.raises(OnDemandCapReached) as caught:
        await request_period(_alice(rooms_db), room.id, "day", _ago(3), run_room=run)
    await _settle()

    assert (caught.value.limit, caught.value.used) == (2, 2)
    assert "2" in str(caught.value)
    assert len(run.calls) == 2
    assert len(_editions(rooms_db, room)) == 2


async def test_at_the_cap_an_existing_edition_is_still_returned(rooms_db):
    room = _report(rooms_db, max_on_demand_per_day=1)
    run = FakeRun()
    edition, _ = await request_period(_alice(rooms_db), room.id, "day", _ago(1), run_room=run)
    await _settle()

    again, started = await request_period(_alice(rooms_db), room.id, "day", _ago(1), run_room=run)

    assert (again.id, started) == (edition.id, False)


async def test_a_cap_of_zero_allows_no_on_demand_run(rooms_db):
    room = _report(rooms_db, max_on_demand_per_day=0)

    with pytest.raises(OnDemandCapReached):
        await request_period(_alice(rooms_db), room.id, "day", _ago(1), run_room=FakeRun())

    assert _editions(rooms_db, room) == []


async def test_scheduled_editions_do_not_count_against_the_cap(rooms_db):
    room = _report(rooms_db, max_on_demand_per_day=1)
    for days in (5, 6, 7):  # a scheduler's editions: no requester
        day = TODAY - timedelta(days=days)
        _published(rooms_db, room, "day", day, day, "Scheduled.")

    _, started = await request_period(_alice(rooms_db), room.id, "day", _ago(1), run_room=FakeRun())
    await _settle()

    assert started


def test_the_default_cap_is_documented_and_positive():
    assert DEFAULT_MAX_ON_DEMAND_PER_DAY > 0
    assert str(DEFAULT_MAX_ON_DEMAND_PER_DAY) in (periods.__doc__ or "")


# ─── refusals ───────────────────────────────────────────────────────────


async def test_a_period_that_is_not_over_is_refused_and_nothing_starts(rooms_db):
    room = _report(rooms_db)
    run = FakeRun()

    for anchor in (TODAY.isoformat(), (TODAY + timedelta(days=3)).isoformat()):
        with pytest.raises(PeriodError, match="not over"):
            await request_period(_alice(rooms_db), room.id, "day", anchor, run_room=run)

    assert run.calls == []
    assert _editions(rooms_db, room) == []


async def test_a_read_only_user_is_refused(rooms_db):
    room = _report(rooms_db)
    dal.add_member(rooms_db.tenant, room.id, rooms_db.bob, added_by=rooms_db.alice)
    viewer = Actor(user_id=rooms_db.bob, role="viewer", tenant_id=rooms_db.tenant)
    run = FakeRun()

    with pytest.raises(RoomForbidden):
        await request_period(viewer, room.id, "day", _ago(1), run_room=run)

    assert run.calls == []
    assert _editions(rooms_db, room) == []


@pytest.mark.parametrize("who", ["stranger", "other_tenant", "service"])
async def test_someone_without_access_cannot_tell_the_room_exists(rooms_db, who):
    room = _report(rooms_db)
    actor = {
        "stranger": Actor(user_id=rooms_db.carol, role="member", tenant_id=rooms_db.tenant),
        "other_tenant": Actor(user_id=rooms_db.alice, role="owner", tenant_id=rooms_db.other),
        "service": Actor(
            user_id=rooms_db.alice, role="owner", tenant_id=rooms_db.tenant, is_service=True
        ),
    }[who]

    with pytest.raises(RoomNotFound):
        await request_period(actor, room.id, "day", _ago(1), run_room=FakeRun())


async def test_an_agent_room_has_no_periods(rooms_db):
    room = dal.create_room(
        rooms_db.tenant, kind="agent", title="Helper", agent_id="main", created_by=rooms_db.alice
    )

    with pytest.raises(PeriodError, match="report"):
        await request_period(_alice(rooms_db), room.id, "day", _ago(1), run_room=FakeRun())


async def test_an_archived_room_starts_nothing(rooms_db):
    room = _report(rooms_db)
    dal.archive_room(rooms_db.tenant, room.id)

    with pytest.raises(RoomForbidden):
        await request_period(_alice(rooms_db), room.id, "day", _ago(1), run_room=FakeRun())


# ─── a week or a month reads its days as data ──────────────────────────


def _week_with_days(db: Any, room: Any) -> Period:
    week = Period(date(2026, 11, 9), date(2026, 11, 15), "week")
    for offset in range(7):
        day = week.start + timedelta(days=offset)
        _published(db, room, "day", day, day, f"Day {day.isoformat()}: 4 orders.")
    # A re-run of Tuesday: the newest edition of a day is the one read.
    tuesday = date(2026, 11, 10)
    _published(db, room, "day", tuesday, tuesday, "Day 2026-11-10 corrected: 5 orders.")
    # Outside the week, a preview, and a building edition: none are read.
    _published(db, room, "day", date(2026, 11, 16), date(2026, 11, 16), "Next Monday.")
    _published(db, room, "day", date(2026, 11, 8), date(2026, 11, 8), "Previous Sunday.")
    preview, _ = dal.create_edition(
        db.tenant,
        room.id,
        grain="day",
        period_start=date(2026, 11, 11),
        period_end=date(2026, 11, 11),
        preview=True,
        requested_by=db.alice,
    )
    dal.publish_edition(db.tenant, preview.id, title="p", summary="PREVIEW ONLY.", blocks=BLOCKS)
    dal.create_edition(
        db.tenant,
        room.id,
        grain="day",
        period_start=date(2026, 11, 12),
        period_end=date(2026, 11, 12),
        requested_by=db.alice,
    )
    # The week's own edition is not a day edition.
    _published(db, room, "week", week.start, week.end, "THE WEEK ITSELF.")
    return week


def test_a_week_reads_the_newest_summary_of_each_day_inside_it(rooms_db):
    room = _report(rooms_db)
    week = _week_with_days(rooms_db, room)

    context = context_for_period(rooms_db.tenant, room.id, week)

    for offset in (0, 2, 3, 4, 5, 6):
        day = week.start + timedelta(days=offset)
        assert f"Day {day.isoformat()}: 4 orders." in context
    assert "Day 2026-11-10 corrected: 5 orders." in context
    assert "Day 2026-11-10: 4 orders." not in context
    for absent in ("Next Monday.", "Previous Sunday.", "PREVIEW ONLY.", "THE WEEK ITSELF."):
        assert absent not in context
    lines = [line for line in context.splitlines() if line.startswith("- 2026-")]
    assert [line[2:12] for line in lines] == [
        (week.start + timedelta(days=d)).isoformat() for d in range(7)
    ]


def test_the_context_is_marked_as_data_and_carries_no_totals(rooms_db):
    room = _report(rooms_db)
    week = _week_with_days(rooms_db, room)

    context = context_for_period(rooms_db.tenant, room.id, week)

    assert "data" in context.lower() and "not instructions" in context.lower()
    assert "29 orders" not in context and "28 orders" not in context
    assert "total" not in context.lower()


def test_the_context_is_capped(rooms_db):
    room = _report(rooms_db)
    month = Period(date(2026, 10, 1), date(2026, 10, 31), "month")
    for offset in range(31):
        day = month.start + timedelta(days=offset)
        _published(rooms_db, room, "day", day, day, f"{day.isoformat()} " + "x" * 2000)

    context = context_for_period(rooms_db.tenant, room.id, month)

    assert len(context) <= CONTEXT_MAX_CHARS
    longest = max(len(line) for line in context.splitlines())
    assert longest <= CONTEXT_SUMMARY_CHARS + len("- 2026-10-01: ")
    assert "omitted" in context  # says what it dropped


def test_another_tenants_or_rooms_days_are_never_read(rooms_db):
    room, other_room = _report(rooms_db), _report(rooms_db)
    week = _week_with_days(rooms_db, other_room)

    assert context_for_period(rooms_db.tenant, room.id, week) == ""
    assert context_for_period(rooms_db.other, other_room.id, week) == ""


def test_a_day_has_no_context(rooms_db):
    room = _report(rooms_db)
    _week_with_days(rooms_db, room)
    day = Period(date(2026, 11, 10), date(2026, 11, 10), "day")

    assert context_for_period(rooms_db.tenant, room.id, day) == ""


async def test_a_week_run_is_handed_its_days(rooms_db):
    room = _report(rooms_db)
    week = last_complete_period("week", "UTC", datetime.now(UTC))
    _published(rooms_db, room, "day", week.start, week.start, "Monday was busy.")
    run = FakeRun()

    _, started = await request_period(
        _alice(rooms_db), room.id, "week", week.start.isoformat(), run_room=run
    )
    await _settle()

    assert started
    assert "Monday was busy." in run.calls[0]["context"]


# ─── the cap is per person ──────────────────────────────────────────────


async def test_each_person_has_their_own_daily_allowance(rooms_db):
    room = _report(rooms_db, max_on_demand_per_day=1)
    dal.add_member(rooms_db.tenant, room.id, rooms_db.bob, added_by=rooms_db.alice)
    bob = Actor(user_id=rooms_db.bob, role="member", tenant_id=rooms_db.tenant)
    run = FakeRun()

    _, alice_started = await request_period(_alice(rooms_db), room.id, "day", _ago(1), run_room=run)
    _, bob_started = await request_period(bob, room.id, "day", _ago(2), run_room=run)
    with pytest.raises(OnDemandCapReached) as alice_again:
        await request_period(_alice(rooms_db), room.id, "day", _ago(3), run_room=run)
    with pytest.raises(OnDemandCapReached):
        await request_period(bob, room.id, "day", _ago(4), run_room=run)
    await _settle()

    assert alice_started and bob_started
    assert (alice_again.value.limit, alice_again.value.used) == (1, 1)
    assert len(run.calls) == 2


# ─── a building edition whose run is gone ───────────────────────────────


def _building(db: Any, room: Any, days_ago: int, *, run_id: str | None = None) -> Any:
    day = TODAY - timedelta(days=days_ago)
    edition, _ = dal.create_edition(
        db.tenant,
        room.id,
        grain="day",
        period_start=day,
        period_end=day,
        requested_by=db.alice,
        run_id=run_id,
    )
    return edition


def _age(db: Any, edition: Any, hours: float) -> None:
    db.cur.execute(
        "UPDATE room_editions SET created_at = now() - make_interval(secs => %s), "
        "updated_at = now() - make_interval(secs => %s) WHERE id = %s",
        (hours * 3600, hours * 3600, edition.id),
    )


def _run_row(db: Any, status: str) -> str:
    db.cur.execute(
        "INSERT INTO agent_runs (tenant_id, agent_id, trigger_type, status, started_at) "
        "VALUES (%s, 'main', 'manual', %s, now()) RETURNING id",
        (db.tenant, status),
    )
    return str(db.cur.fetchone()[0])


def _status(db: Any, edition: Any) -> tuple[str, str | None]:
    db.cur.execute("SELECT status, error FROM room_editions WHERE id = %s", (edition.id,))
    return db.cur.fetchone()


async def test_a_cancelled_run_fails_its_edition(rooms_db):
    room = _report(rooms_db)
    gate = asyncio.Event()

    async def blocked(room: Any, edition: Any, *, period: Period, context: str) -> None:
        await gate.wait()

    edition, started = await request_period(
        _alice(rooms_db), room.id, "day", _ago(1), run_room=blocked
    )
    await asyncio.sleep(0.05)
    for task in periods.pending_runs():
        task.cancel()
    await _settle()

    status, error = _status(rooms_db, edition)
    assert started
    assert status == "failed"
    assert "interrupted" in error


def test_at_engine_start_an_edition_from_before_the_boot_is_failed(rooms_db):
    room = _report(rooms_db)
    orphan = _building(rooms_db, room, 1)

    reaped = reap_stale_editions(
        rooms_db.tenant, booted_at=datetime.now(UTC) + timedelta(seconds=5)
    )

    assert [e.id for e in reaped] == [orphan.id]
    status, error = _status(rooms_db, orphan)
    assert status == "failed"
    assert "restarted" in error


def test_a_recent_edition_after_the_boot_is_left_building(rooms_db):
    room = _report(rooms_db)
    edition = _building(rooms_db, room, 1)

    assert (
        reap_stale_editions(rooms_db.tenant, booted_at=datetime.now(UTC) - timedelta(hours=1)) == []
    )
    assert _status(rooms_db, edition)[0] == "building"


def test_an_edition_building_past_the_age_bound_is_failed(rooms_db):
    room = _report(rooms_db)
    edition = _building(rooms_db, room, 1)
    _age(rooms_db, edition, STALE_BUILDING_SECONDS / 3600 + 0.1)

    reaped = reap_stale_editions(rooms_db.tenant)

    assert [e.id for e in reaped] == [edition.id]
    assert "no run" in _status(rooms_db, edition)[1]


def test_the_run_rows_state_decides_when_there_is_one(rooms_db):
    room = _report(rooms_db)
    running = _building(rooms_db, room, 1, run_id=_run_row(rooms_db, "running"))
    _age(rooms_db, running, STALE_BUILDING_SECONDS / 3600 + 1)  # old, but its run is alive
    ended = _building(rooms_db, room, 2, run_id=_run_row(rooms_db, "timeout"))  # recent, but over

    reaped = reap_stale_editions(
        rooms_db.tenant, booted_at=datetime.now(UTC) + timedelta(seconds=5)
    )

    assert [e.id for e in reaped] == [ended.id]
    assert _status(rooms_db, running)[0] == "building"
    assert "timeout" in _status(rooms_db, ended)[1]


async def test_a_run_alive_in_this_process_is_never_reaped(rooms_db):
    room = _report(rooms_db)
    gate = asyncio.Event()

    async def blocked(room: Any, edition: Any, *, period: Period, context: str) -> None:
        await gate.wait()

    edition, _ = await request_period(_alice(rooms_db), room.id, "day", _ago(1), run_room=blocked)
    _age(rooms_db, edition, STALE_BUILDING_SECONDS / 3600 + 1)

    reaped = reap_stale_editions(
        rooms_db.tenant, booted_at=datetime.now(UTC) + timedelta(seconds=5)
    )
    gate.set()
    await _settle()

    assert reaped == []
    assert _status(rooms_db, edition)[0] == "building"


def test_reaping_stays_inside_its_tenant_and_room(rooms_db):
    room, other_room = _report(rooms_db), _report(rooms_db)
    mine, theirs = _building(rooms_db, room, 1), _building(rooms_db, other_room, 1)
    boot = datetime.now(UTC) + timedelta(seconds=5)

    assert reap_stale_editions(rooms_db.other, booted_at=boot) == []
    assert [
        e.id for e in reap_stale_editions(rooms_db.tenant, room_id=room.id, booted_at=boot)
    ] == [mine.id]
    assert _status(rooms_db, theirs)[0] == "building"


async def test_asking_again_for_an_orphaned_period_starts_a_new_run(rooms_db, monkeypatch):
    room = _report(rooms_db)
    orphan = _building(rooms_db, room, 1)
    # The engine restarted after the orphan was claimed.
    boot = (datetime.now(UTC) + timedelta(seconds=5)).isoformat()
    monkeypatch.setenv("ROBOTHOR_DAEMON_START_TS", boot)
    run = FakeRun()

    edition, started = await request_period(_alice(rooms_db), room.id, "day", _ago(1), run_room=run)
    await _settle()

    assert started
    assert edition.id != orphan.id
    assert _status(rooms_db, orphan)[0] == "failed"
    assert len(run.calls) == 1
