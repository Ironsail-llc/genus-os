"""Natural-language cron schedule parser (Rip 8).

Adapted from Hermes ``cron/jobs.py:188-296`` (``parse_duration``,
``parse_schedule``). Accepts four input shapes:

* duration shorthand — ``"30m"``, ``"2h"``, ``"1d"`` (one-shot, fires after the delay)
* interval shorthand — ``"every 30m"``, ``"every 2 hours"``
* cron expression — five fields ``"0 9 * * *"``
* ISO timestamp — ``"2026-06-01T15:00:00Z"`` (one-shot at the given time)

Output is a structured dict the engine scheduler can consume::

    {"kind": "once",     "fire_at": datetime}
    {"kind": "interval", "every_seconds": int}
    {"kind": "cron",     "expression": str}

Invalid inputs raise ``ValueError``. Hardening for prompt-injection is in
``cron_safety.py``; this module is just the parser.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

_DURATION_RE = re.compile(r"^(\d+)\s*([smhdw])$", re.IGNORECASE)
_NATURAL_INTERVAL_RE = re.compile(
    r"^every\s+(\d+)\s*"
    r"(s|sec|secs|second|seconds|m|min|mins|minute|minutes|"
    r"h|hr|hrs|hour|hours|d|day|days|w|wk|wks|week|weeks)$",
    re.IGNORECASE,
)
_CRON_RE = re.compile(r"^[\d\*/,\-]+(\s+[\d\*/,\-]+){4}$")

_UNIT_TO_SECONDS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "w": 604800,
    "wk": 604800,
    "wks": 604800,
    "week": 604800,
    "weeks": 604800,
}

MIN_INTERVAL_SECONDS = 60  # Reject any schedule that fires more than once a minute


def parse_duration(text: str) -> int:
    """Parse a shorthand like ``'30m'`` to seconds."""
    m = _DURATION_RE.match(text.strip())
    if not m:
        raise ValueError(f"unrecognized duration shorthand: {text!r}")
    n, unit = int(m.group(1)), m.group(2).lower()
    return n * _UNIT_TO_SECONDS[unit]


def parse_schedule(text: str) -> dict[str, Any]:
    """Translate a natural-language schedule to a structured dict.

    Raises ``ValueError`` when no shape matches or when the resulting
    interval would be sub-minute (anti-runaway guard).
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("schedule cannot be empty")

    # 1. Natural-language interval — "every 30m", "every 2 hours"
    nat = _NATURAL_INTERVAL_RE.match(raw)
    if nat:
        n, unit = int(nat.group(1)), nat.group(2).lower()
        secs = n * _UNIT_TO_SECONDS[unit]
        if secs < MIN_INTERVAL_SECONDS:
            raise ValueError(
                f"interval {raw!r} is shorter than the {MIN_INTERVAL_SECONDS}s minimum"
            )
        return {"kind": "interval", "every_seconds": secs}

    # 2. Duration shorthand alone — fires once in N from now
    if _DURATION_RE.match(raw):
        secs = parse_duration(raw)
        return {"kind": "once", "fire_at": datetime.now(UTC) + timedelta(seconds=secs)}

    # 3. ISO timestamp — fires once at the given instant
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        when = None
    if when is not None:
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return {"kind": "once", "fire_at": when}

    # 4. Cron expression — five fields
    if _CRON_RE.match(raw):
        return {"kind": "cron", "expression": raw}

    raise ValueError(f"unrecognized schedule: {text!r}")
