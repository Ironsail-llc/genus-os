"""Relative dates in a message, counted from when the message was sent.

"I bought it yesterday" said on 2023-05-20 happened on 2023-05-19, whenever it
is read. :func:`resolve_relative_date` does that arithmetic for the expressions
that name one exact day (yesterday, tomorrow, N days or weeks ago, in N days,
last or next <weekday>, the day before yesterday). Vague spans ("last week",
"next month", "recently") name no day and resolve to nothing. When a text names
two different days, it is not known which one a fact is about, so nothing is
returned either. Deterministic; no model call.

:func:`relative_days` gives every day named, for a caller that weighs them
itself (conversation extraction: ``conversation_facts._dated``).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_NUMBERS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_N = r"(\d{1,3}|a|an|one|two|three|four|five|six|seven|eight|nine|ten)"
_UNIT = r"(day|week)s?"
_WD = "(" + "|".join(_WEEKDAYS) + ")"

# Longest first, and each match is blanked out, so "the day before yesterday"
# is not also read as "yesterday".
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(the\s+)?day\s+before\s+yesterday\b"), "fixed:-2"),
    (re.compile(r"\b(the\s+)?day\s+after\s+tomorrow\b"), "fixed:2"),
    (re.compile(rf"\b{_N}\s+{_UNIT}\s+ago\b"), "ago"),
    (re.compile(rf"\bin\s+{_N}\s+{_UNIT}\b"), "in"),
    (re.compile(rf"\blast\s+{_WD}\b"), "last_wd"),
    (re.compile(rf"\bnext\s+{_WD}\b"), "next_wd"),
    (re.compile(r"\byesterday\b"), "fixed:-1"),
    (re.compile(r"\btomorrow\b"), "fixed:1"),
    (re.compile(r"\b(today|tonight|this\s+(morning|afternoon|evening))\b"), "fixed:0"),
]


def _count(word: str) -> int:
    return int(word) if word.isdigit() else _NUMBERS[word]


def _days(unit: str) -> int:
    return 7 if unit.startswith("week") else 1


def _resolve(kind: str, match: re.Match[str], day: date) -> date:
    if kind.startswith("fixed:"):
        return day + timedelta(days=int(kind.split(":", 1)[1]))
    if kind == "ago":
        return day - timedelta(days=_count(match.group(1)) * _days(match.group(2)))
    if kind == "in":
        return day + timedelta(days=_count(match.group(1)) * _days(match.group(2)))
    target = _WEEKDAYS.index(match.group(1))
    if kind == "last_wd":
        back = (day.weekday() - target) % 7 or 7
        return day - timedelta(days=back)
    ahead = (target - day.weekday()) % 7 or 7
    return day + timedelta(days=ahead)


#: The kind of "today", "tonight", "this morning": it frames a turn more often
#: than it dates the thing a fact is about.
_TODAY = "fixed:0"


def relative_days(text: str, at: datetime | None, *, include_today: bool = True) -> set[date]:
    """Every day the relative expressions in ``text`` name, counted from ``at``.

    ``include_today=False`` leaves out "today" and its kind.
    """
    if at is None or not text:
        return set()
    day = at.date()
    rest = text.lower()
    found: set[date] = set()
    for pattern, kind in _PATTERNS:
        if include_today or kind != _TODAY:
            for match in pattern.finditer(rest):
                found.add(_resolve(kind, match, day))
        rest = pattern.sub(lambda m: " " * len(m.group(0)), rest)
    return found


def resolve_relative_date(text: str, at: datetime | None) -> date | None:
    """The one day the relative expressions in ``text`` name, counted from ``at``.

    ``None`` when ``at`` is unknown, when the text names no exact day, or when
    it names more than one.
    """
    found = relative_days(text, at)
    return found.pop() if len(found) == 1 else None


__all__ = ["relative_days", "resolve_relative_date"]
