"""Standard crontab day-of-week numbering (0/7 = Sunday) for APScheduler.

APScheduler 3.x ``CronTrigger.from_crontab`` keeps its own numbering
(0 = Monday), so "0 9 * * 1" fired on Tuesdays and "0 4 * * 0" on Mondays.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from robothor.engine.crontab import cron_trigger

TZ = ZoneInfo("America/New_York")
FRIDAY = datetime(2026, 10, 23, 15, 0, tzinfo=TZ)


def _next(expr: str) -> datetime:
    t = cron_trigger(expr, timezone=TZ)
    nxt = t.get_next_fire_time(None, FRIDAY)
    assert nxt is not None
    return nxt


@pytest.mark.parametrize(
    ("expr", "weekday"),
    [
        ("0 9 * * 1", "Monday"),
        ("0 4 * * 0", "Sunday"),
        ("0 4 * * 7", "Sunday"),
        ("0 9 * * 6", "Saturday"),
        ("0 9 * * mon", "Monday"),
        ("0 9 * * SUN", "Sunday"),
    ],
)
def test_weekday_numbers_follow_crontab(expr, weekday):
    assert _next(expr).strftime("%A") == weekday


def test_range_and_list():
    t = cron_trigger("30 10 * * 1-5", timezone=TZ)
    days = set()
    when = FRIDAY
    for _ in range(10):
        when = t.get_next_fire_time(None, when)
        days.add(when.strftime("%a"))
        when = when.replace(minute=31)
    assert days == {"Mon", "Tue", "Wed", "Thu", "Fri"}
    assert _next("0 9 * * 0,3").strftime("%A") == "Sunday"


def test_step_counts_from_sunday():
    t = cron_trigger("0 9 * * */2", timezone=TZ)  # Sun, Tue, Thu, Sat
    days = set()
    when = FRIDAY
    for _ in range(8):
        when = t.get_next_fire_time(None, when)
        days.add(when.strftime("%a"))
        when = when.replace(minute=1)
    assert days == {"Sun", "Tue", "Thu", "Sat"}


def test_star_and_other_fields_untouched():
    assert _next("15 7 * * *") == datetime(2026, 10, 24, 7, 15, tzinfo=TZ)


@pytest.mark.parametrize("expr", ["0 9 * * 8", "0 9 * *", "nonsense"])
def test_invalid_raises_value_error(expr):
    with pytest.raises(ValueError):
        cron_trigger(expr)
