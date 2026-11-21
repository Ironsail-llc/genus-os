"""Period maths for report editions (story B1).

A person picks a day, a week or a month and gets that period's edition. The
period is a run of whole calendar days in the ROOM's time zone, end inclusive
(the same dates ``room_editions.period_start`` / ``period_end`` store). Weeks
start on Monday. A period that is not over yet is never offered: an edition of
it would be a partial figure presented as a whole one.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, date, datetime, timedelta

import pytest

from robothor.rooms.periods import (
    DEFAULT_MAX_ON_DEMAND_PER_DAY,
    MAX_CUSTOM_DAYS,
    Period,
    PeriodError,
    last_complete_period,
    resolve_period,
)

LONDON = "Europe/London"
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)  # a Saturday


# ─── day, week, month ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("grain", "anchor", "start", "end"),
    [
        ("day", "2026-11-18", date(2026, 11, 18), date(2026, 11, 18)),
        # Wednesday -> its Monday-to-Sunday week.
        ("week", "2026-11-11", date(2026, 11, 9), date(2026, 11, 15)),
        # A Monday and a Sunday are the edges of their own week.
        ("week", "2026-11-09", date(2026, 11, 9), date(2026, 11, 15)),
        ("week", "2026-11-15", date(2026, 11, 9), date(2026, 11, 15)),
        ("month", "2026-10", date(2026, 10, 1), date(2026, 10, 31)),
        ("month", "2026-10-17", date(2026, 10, 1), date(2026, 10, 31)),
        # Month ends: 30 days, a leap February, a plain February.
        ("month", "2026-09", date(2026, 9, 1), date(2026, 9, 30)),
        ("month", "2024-02", date(2024, 2, 1), date(2024, 2, 29)),
        ("month", "2026-02", date(2026, 2, 1), date(2026, 2, 28)),
        # Year end: December, and a week that straddles New Year.
        ("month", "2025-12", date(2025, 12, 1), date(2025, 12, 31)),
        ("week", "2025-12-31", date(2025, 12, 29), date(2026, 1, 4)),
        ("custom", "2026-10-03/2026-10-09", date(2026, 10, 3), date(2026, 10, 9)),
    ],
)
def test_an_anchor_resolves_to_the_whole_period_containing_it(grain, anchor, start, end):
    period = resolve_period(grain, LONDON, anchor, now=NOW)

    assert period == Period(start, end, grain)
    assert period.days == (end - start).days + 1


def test_an_anchor_may_be_a_date():
    assert resolve_period("week", "UTC", date(2026, 11, 11), now=NOW) == Period(
        date(2026, 11, 9), date(2026, 11, 15), "week"
    )


def test_a_custom_range_may_be_given_as_a_pair_or_a_mapping():
    pair = resolve_period("custom", "UTC", (date(2026, 10, 1), date(2026, 10, 2)), now=NOW)
    mapping = resolve_period("custom", "UTC", {"start": "2026-10-01", "end": "2026-10-02"}, now=NOW)

    assert pair == mapping == Period(date(2026, 10, 1), date(2026, 10, 2), "custom")


@pytest.mark.parametrize(
    ("grain", "start", "end"),
    [
        ("day", date(2026, 11, 20), date(2026, 11, 20)),
        # Saturday: the current week is not over, so the last whole one.
        ("week", date(2026, 11, 9), date(2026, 11, 15)),
        ("month", date(2026, 10, 1), date(2026, 10, 31)),
    ],
)
def test_the_last_complete_period(grain, start, end):
    assert last_complete_period(grain, LONDON, NOW) == Period(start, end, grain)


@pytest.mark.parametrize("anchor", [None, "", "latest"])
def test_no_anchor_means_the_last_complete_period(anchor):
    assert resolve_period("week", LONDON, anchor, now=NOW) == last_complete_period(
        "week", LONDON, NOW
    )


def test_on_a_monday_last_week_has_just_ended():
    monday = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)

    assert last_complete_period("week", "UTC", monday) == Period(
        date(2026, 11, 9), date(2026, 11, 15), "week"
    )


def test_on_new_years_day_the_last_month_is_last_years_december():
    new_year = datetime(2027, 1, 1, 0, 30, tzinfo=UTC)

    assert last_complete_period("month", "UTC", new_year) == Period(
        date(2026, 12, 1), date(2026, 12, 31), "month"
    )
    assert last_complete_period("day", "UTC", new_year) == Period(
        date(2026, 12, 31), date(2026, 12, 31), "day"
    )


# ─── daylight saving ────────────────────────────────────────────────────


def test_the_spring_forward_day_is_23_hours_and_its_week_one_hour_short():
    day = resolve_period("day", LONDON, "2026-03-29", now=NOW)
    week = resolve_period("week", LONDON, "2026-03-29", now=NOW)

    start, end = day.bounds(LONDON)
    assert end - start == timedelta(hours=23)
    assert week == Period(date(2026, 3, 23), date(2026, 3, 29), "week")
    week_start, week_end = week.bounds(LONDON)
    assert week_end - week_start == timedelta(days=7) - timedelta(hours=1)
    # Local midnights, not UTC ones.
    assert week_start.astimezone(UTC) == datetime(2026, 3, 23, 0, 0, tzinfo=UTC)
    assert week_end.astimezone(UTC) == datetime(2026, 3, 29, 23, 0, tzinfo=UTC)


def test_the_fall_back_day_is_25_hours():
    day = resolve_period("day", LONDON, "2026-10-25", now=NOW)
    month = resolve_period("month", LONDON, "2026-10", now=NOW)

    start, end = day.bounds(LONDON)
    assert end - start == timedelta(hours=25)
    month_start, month_end = month.bounds(LONDON)
    assert month_end - month_start == timedelta(days=31, hours=1)


@pytest.mark.parametrize(
    ("now", "yesterday"),
    [
        # 00:30 GMT on the change day: still the 29th locally.
        (datetime(2026, 3, 29, 0, 30, tzinfo=UTC), date(2026, 3, 28)),
        # 23:30 UTC on the 29th is 00:30 BST on the 30th.
        (datetime(2026, 3, 29, 23, 30, tzinfo=UTC), date(2026, 3, 29)),
        # 23:30 UTC on 25 Oct is 23:30 GMT, after the clocks went back.
        (datetime(2026, 10, 25, 23, 30, tzinfo=UTC), date(2026, 10, 24)),
        (datetime(2026, 10, 24, 23, 30, tzinfo=UTC), date(2026, 10, 24)),
    ],
)
def test_yesterday_follows_local_midnight_across_a_clock_change(now, yesterday):
    assert last_complete_period("day", LONDON, now) == Period(yesterday, yesterday, "day")


# ─── the room's zone, not the server's ──────────────────────────────────


@pytest.fixture
def server_in_los_angeles():
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


@pytest.mark.usefixtures("server_in_los_angeles")
def test_the_room_time_zone_decides_the_day_whatever_the_servers_is():
    # 06:00 UTC on 1 June: 15:00 in Tokyo, 23:00 on 31 May in Los Angeles.
    now = datetime(2026, 6, 1, 6, 0, tzinfo=UTC)

    assert last_complete_period("day", "Asia/Tokyo", now).start == date(2026, 5, 31)
    assert last_complete_period("day", "America/Los_Angeles", now).start == date(2026, 5, 30)
    # Tokyo's May is over; Los Angeles's is not.
    assert last_complete_period("month", "Asia/Tokyo", now).start == date(2026, 5, 1)
    assert last_complete_period("month", "America/Los_Angeles", now).start == date(2026, 4, 1)


def test_the_same_instant_is_a_different_week_on_either_side_of_the_date_line():
    # Sunday 23:00 UTC is already Monday in Auckland: last week has ended there.
    now = datetime(2026, 11, 15, 23, 0, tzinfo=UTC)

    assert last_complete_period("week", "Pacific/Auckland", now).end == date(2026, 11, 15)
    assert last_complete_period("week", "UTC", now).end == date(2026, 11, 8)


def test_a_naive_now_is_refused():
    with pytest.raises(ValueError, match="time zone"):
        last_complete_period("day", "UTC", datetime(2026, 11, 21, 12, 0))  # noqa: DTZ001


# ─── refusals ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("grain", "anchor"),
    [
        ("day", "2026-11-21"),  # today: not over yet
        ("day", "2026-12-01"),
        ("week", "2026-11-16"),  # this week
        ("month", "2026-11"),  # this month
        ("custom", "2026-11-01/2026-11-21"),  # ends today
    ],
)
def test_a_period_that_has_not_ended_is_refused(grain, anchor):
    with pytest.raises(PeriodError, match="not over"):
        resolve_period(grain, LONDON, anchor, now=NOW)


@pytest.mark.parametrize("tz", ["Mars/Olympus", "", "../../etc/passwd", "/etc/localtime", None])
def test_a_time_zone_that_is_not_an_iana_name_is_refused(tz):
    with pytest.raises(PeriodError, match="time zone"):
        resolve_period("day", tz, "2026-11-01", now=NOW)


@pytest.mark.parametrize(
    ("grain", "anchor"),
    [
        ("year", "2025"),
        ("day", "yesterday"),
        ("day", "2026-02-30"),
        ("month", "2026-13"),
        ("custom", "2026-10-09/2026-10-03"),  # backwards
        ("custom", "2026-10-09"),  # one end only
        ("custom", None),  # a custom range has no "latest"
        ("day", 20261101),
    ],
)
def test_a_malformed_request_is_refused(grain, anchor):
    with pytest.raises(PeriodError):
        resolve_period(grain, "UTC", anchor, now=NOW)


def test_a_custom_range_has_a_maximum_length():
    start = date(2026, 1, 1)
    longest = start + timedelta(days=MAX_CUSTOM_DAYS - 1)

    assert resolve_period("custom", "UTC", (start, longest), now=NOW).days == MAX_CUSTOM_DAYS
    with pytest.raises(PeriodError, match=str(MAX_CUSTOM_DAYS)):
        resolve_period("custom", "UTC", (start, longest + timedelta(days=1)), now=NOW)


def test_a_period_is_a_value_that_cannot_run_backwards():
    period = Period(date(2026, 11, 9), date(2026, 11, 15), "week")

    assert {period: 1}[Period(date(2026, 11, 9), date(2026, 11, 15), "week")] == 1
    with pytest.raises(ValueError, match="before"):
        Period(date(2026, 11, 15), date(2026, 11, 9), "week")


def test_a_date_and_time_is_not_a_date():
    with pytest.raises(PeriodError, match="not a date and time"):
        resolve_period("day", "UTC", datetime(2026, 11, 1, 9, 0, tzinfo=UTC), now=NOW)


def test_last_complete_period_refuses_an_unknown_grain():
    with pytest.raises(PeriodError, match="grain"):
        last_complete_period("year", "UTC", NOW)


# ─── the daily cap setting and the room runner ──────────────────────────


@pytest.mark.parametrize(
    ("value", "cap"),
    [
        (None, DEFAULT_MAX_ON_DEMAND_PER_DAY),
        (3, 3),
        ("4", 4),
        (2.0, 2),
        (-1, 0),
        (0, 0),
        ("many", DEFAULT_MAX_ON_DEMAND_PER_DAY),
        (True, DEFAULT_MAX_ON_DEMAND_PER_DAY),
        ([5], DEFAULT_MAX_ON_DEMAND_PER_DAY),
    ],
)
def test_the_cap_setting_falls_back_to_the_documented_default(value, cap):
    from robothor.rooms.models import Room
    from robothor.rooms.periods import _cap

    settings = {} if value is None else {"max_on_demand_per_day": value}
    room = Room("test-tenant", "r", "report", "Daily", "main", "k", "u", settings=settings)

    assert _cap(room) == cap


def test_the_room_runner_is_the_room_runs_module(monkeypatch):
    import sys
    import types

    from robothor.rooms.periods import resolve_run_room

    async def run_room(room, edition, *, period, context):  # pragma: no cover - never called
        return None

    module = types.ModuleType("robothor.rooms.runs")
    module.run_room = run_room  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "robothor.rooms.runs", module)

    assert resolve_run_room() is run_room


def test_without_a_room_runs_module_the_runner_is_unavailable(monkeypatch):
    import sys

    from robothor.rooms.periods import RoomRunUnavailable, resolve_run_room

    monkeypatch.setitem(sys.modules, "robothor.rooms.runs", None)  # import raises ImportError

    with pytest.raises(RoomRunUnavailable, match="not available"):
        resolve_run_room()
