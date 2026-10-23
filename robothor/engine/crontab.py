"""Crontab expressions → APScheduler triggers with standard weekday numbering.

APScheduler 3.x ``CronTrigger.from_crontab`` passes the day-of-week field
through with its own numbering (0 = Monday … 6 = Sunday), while crontab and
every manifest in this repo mean 0/7 = Sunday, 1 = Monday. "0 9 * * 1" was
firing on Tuesdays. Numeric weekday values are rewritten to names here, which
both conventions read the same way. Build every crontab trigger through
:func:`cron_trigger`.
"""

from __future__ import annotations

from typing import Any

from apscheduler.triggers.cron import CronTrigger

_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")


def _weekday(value: str) -> int:
    n = int(value)
    if not 0 <= n <= 7:
        raise ValueError(f"day-of-week value out of range 0-7: {value!r}")
    return n % 7


def _weekdays(part: str) -> set[int]:
    rng, _, step_s = part.partition("/")
    step = int(step_s) if step_s else 1
    if step < 1:
        raise ValueError(f"invalid day-of-week step: {part!r}")
    if rng == "*":
        lo, hi = 0, 6
    elif "-" in rng:
        a, b = rng.split("-", 1)
        lo, hi = int(a), int(b)
        if not (0 <= lo <= 7 and 0 <= hi <= 7) or lo > hi:
            raise ValueError(f"invalid day-of-week range: {part!r}")
    else:
        lo = hi = int(rng)
        if step_s:
            hi = 6
    return {_weekday(str(v)) for v in range(lo, hi + 1, step)}


def crontab_day_of_week(field: str) -> str:
    """A crontab day-of-week field in names APScheduler reads unambiguously."""
    if field == "*" or any(c.isalpha() for c in field):
        return field
    try:
        days: set[int] = set()
        for part in field.split(","):
            days |= _weekdays(part)
    except ValueError as e:
        raise ValueError(f"invalid day-of-week field {field!r}: {e}") from e
    return ",".join(_NAMES[d] for d in sorted(days))


def cron_trigger(expr: str, timezone: Any = None) -> CronTrigger:
    """``CronTrigger.from_crontab`` with crontab's 0/7 = Sunday weekday numbering."""
    fields = str(expr).split()
    if len(fields) != 5:
        raise ValueError(f"Wrong number of fields; got {len(fields)}, expected 5")
    fields[4] = crontab_day_of_week(fields[4])
    return CronTrigger.from_crontab(" ".join(fields), timezone=timezone)
