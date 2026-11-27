"""Relative dates in a message, counted from when the message was sent.

"I bought it yesterday" said on 2023-05-20 happened on 2023-05-19, whenever it
is read. :func:`relative_days` does that arithmetic for the expressions that
name one exact day, counted from the message's own local day (the ``at`` it is
given, in the zone it carries):

* yesterday, today / tonight / this morning, tomorrow, the day before
  yesterday, the day after tomorrow;
* N days or weeks ago (N from 1 to 60, in digits or a word up to ten);
* in N days or weeks, only when the clause points forward ("I'm flying to Rome
  in two weeks", "it's due in 3 weeks", "in 2 days' time", "we will move in 10
  days"): otherwise "in two days" is a duration ("I finished it in two days");
* last <weekday> (the most recent one before the message's day).

Everything else names no day and resolves to nothing: vague spans ("last week",
"next month", "a few days ago"), "next <weekday>" and "this <weekday>" (which
week is meant depends on the speaker), anything inside quotation marks, and
titles ("The Day After Tomorrow", "Yesterday by the Beatles").

:func:`days_in_phrase` resolves only the expressions inside one phrase of a
text, reading the clause around it, so one clause of a message never dates a
fact drawn from another. Deterministic; no model call.
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
#: Larger counts are rare in speech and more often a typo or a duration.
_MAX_COUNT = 60
_N = r"(\d{1,3}|an?|one|two|three|four|five|six|seven|eight|nine|ten)"
_UNIT = r"(day|week)s?"
_WD = "(" + "|".join(_WEEKDAYS) + ")"

# Longest first; each match is blanked out, so "the day before yesterday" is
# not also read as "yesterday".
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(the\s+)?day\s+before\s+yesterday\b", re.IGNORECASE), "fixed:-2"),
    (re.compile(r"\b(the\s+)?day\s+after\s+tomorrow\b", re.IGNORECASE), "fixed:2"),
    (re.compile(rf"\b{_N}\s+{_UNIT}\s+ago\b", re.IGNORECASE), "ago"),
    (re.compile(rf"\bin\s+{_N}\s+{_UNIT}\b", re.IGNORECASE), "in"),
    (re.compile(rf"\blast\s+{_WD}\b", re.IGNORECASE), "last_wd"),
    # Matched only to be blanked: which week "next/this <weekday>" means is the
    # speaker's, so it names no day ("next monday week" included).
    (re.compile(rf"\b(next|this)\s+{_WD}(\s+week)?\b", re.IGNORECASE), "ambiguous"),
    (re.compile(r"\byesterday\b", re.IGNORECASE), "fixed:-1"),
    (re.compile(r"\btomorrow\b", re.IGNORECASE), "fixed:1"),
    (
        re.compile(r"\b(today|tonight|this\s+(morning|afternoon|evening))\b", re.IGNORECASE),
        "fixed:0",
    ),
]

#: Quoted spans: double or curly quotes, or single quotes that open after a
#: space (so "I'm" and "days' time" are not quotes).
_QUOTED = re.compile(r"\"[^\"]*\"|“[^”]*”|‘[^’]*’|(?:(?<=\s)|^)'[^']*'(?=\W|$)")

#: Signs that "in N days/weeks" points forward, in the same clause.
_FORWARD = re.compile(
    r"\b(will|shall|won't|going\s+to|gonna|due|from\s+now|time|"
    r"(i'm|i\s+am|we're|we\s+are|you're|you\s+are|they're|they\s+are|he's|she's)\s+\w+ing)\b"
    r"|'ll\b",
    re.IGNORECASE,
)
#: "in a week long course", "in ten days cycles": a duration describing a noun.
_DURATION_NOUN = re.compile(
    r"^[\s'-]*(long|old|cycles?|periods?|stints?|programmes?|programs?|courses?|trials?)\b",
    re.IGNORECASE,
)
_CLAUSE_END = re.compile(r"[.;!?\n]|,\s+(?:but|and\s+then|no\s+wait)\b", re.IGNORECASE)


def _count(word: str) -> int | None:
    n = int(word) if word.isdigit() else _NUMBERS.get(word.lower())
    return n if n is not None and 1 <= n <= _MAX_COUNT else None


def _days(unit: str) -> int:
    return 7 if unit.lower().startswith("week") else 1


def _clause(text: str, start: int, end: int) -> str:
    lo = 0
    for m in _CLAUSE_END.finditer(text):
        if m.end() <= start:
            lo = m.end()
        elif m.start() >= end:
            return text[lo : m.start()]
    return text[lo:]


def _is_title(original: str, match: re.Match[str]) -> bool:
    """A capitalised multi-word name ("The Day After Tomorrow"), or "Yesterday by X"."""
    words = original[match.start() : match.end()].split()
    inner = [w for w in words[1:] if w.lower().strip(",.") not in _WEEKDAYS]
    if any(w[:1].isupper() for w in inner):
        return True
    after = original[match.end() :]
    return words[:1] == ["Yesterday"] and re.match(r"\s+by\s+(the\s+)?[A-Z]", after) is not None


def _resolve(kind: str, match: re.Match[str], day: date, text: str) -> date | None:
    if kind == "ambiguous":
        return None
    if kind.startswith("fixed:"):
        return day + timedelta(days=int(kind.split(":", 1)[1]))
    if kind in ("ago", "in"):
        n = _count(match.group(1))
        if n is None:
            return None
        if kind == "in":
            clause = _clause(text, match.start(), match.end())
            if _DURATION_NOUN.match(text[match.end() :]) or not _FORWARD.search(clause):
                return None
            return day + timedelta(days=n * _days(match.group(2)))
        return day - timedelta(days=n * _days(match.group(2)))
    target = _WEEKDAYS.index(match.group(1).lower())
    back = (day.weekday() - target) % 7 or 7
    return day - timedelta(days=back)


def _matches(text: str, at: datetime) -> list[tuple[int, int, date]]:
    """``(start, end, day)`` of every expression in ``text`` that names a day."""
    day = at.date()
    rest = _QUOTED.sub(lambda m: " " * len(m.group(0)), text)
    out: list[tuple[int, int, date]] = []
    for pattern, kind in _PATTERNS:
        for match in pattern.finditer(rest):
            if _is_title(text, match):
                continue
            resolved = _resolve(kind, match, day, rest)
            if resolved is not None:
                out.append((match.start(), match.end(), resolved))
        rest = pattern.sub(lambda m: " " * len(m.group(0)), rest)
    return out


def relative_days(text: str, at: datetime | None) -> set[date]:
    """Every day the relative expressions in ``text`` name, counted from ``at``."""
    if at is None or not text:
        return set()
    return {d for _s, _e, d in _matches(text, at)}


def days_in_phrase(text: str, phrase: str, at: datetime | None) -> set[date] | None:
    """The days named inside ``phrase`` where it occurs in ``text``.

    ``None`` when the phrase does not occur in the text (case and spacing
    ignored). Expressions are read in their own clause of ``text`` (so "in two
    weeks" knows whether "I'm flying" precedes it), but only those overlapping
    the phrase count.
    """
    if at is None or not text or not phrase or not phrase.strip():
        return None
    words = [re.escape(w) for w in phrase.split()]
    found = re.search(r"\s+".join(words), text, re.IGNORECASE)
    if found is None:
        return None
    return {d for s, e, d in _matches(text, at) if s < found.end() and e > found.start()}


def resolve_relative_date(text: str, at: datetime | None) -> date | None:
    """The one day ``text`` names, counted from ``at``; ``None`` for none or several."""
    found = relative_days(text, at)
    return found.pop() if len(found) == 1 else None


__all__ = ["days_in_phrase", "relative_days", "resolve_relative_date"]
