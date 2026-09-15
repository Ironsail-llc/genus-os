"""How a Helm route reads a scalar out of a URL, and refuses one it cannot.

Two kinds of value, four routers, and one copy of each — because the copies
were already disagreeing. Every module that took an id or a page size spelled
it ``int(str(value))`` with a range check, and Python's ``int`` accepts a good
deal more than a URL ever names:

    POST /api/memory/facts/1_0/forget  ->  int("1_0") == 10  ->  fact 10 is forgotten

The audit row then said ``fact_id: 10`` while its ``path`` said
``/api/memory/facts/1_0/forget`` — two answers to "which fact did the operator
forget" inside one audit record. ``+7``, ``007`` and ``" 5 "`` went the same
way. So the SHAPE is checked first, against what the URL is allowed to contain,
and ``int`` is only ever called on a string that is already nothing but digits.

The same reasoning applies to a timestamp: ``since=notatimestamp`` reached
psycopg2 and came back as a 500 — a caller's error rendered as an appliance
fault, in an API whose whole contract is that those are different things.

Every refusal here is the flat ``{"detail": "<sentence>"}`` the Helm was
promised. That is the other reason this is a module rather than four pydantic
``Field`` caps: FastAPI enforces those before the handler runs and renders them
as ``{"detail": [ {...} ]}``, a second body shape for the same class of error.
"""

from __future__ import annotations

import re
from datetime import datetime

from fastapi import HTTPException

#: A positive integer as a URL may spell one: no sign, no underscore, no
#: leading zero, no whitespace, at most ten digits. Ten because that covers
#: every id column the bridge exposes (``memory_facts.id`` is a 32-bit
#: ``SERIAL``; ``feature_flag_audit.id`` a ``BIGSERIAL`` whose live values are
#: far below this) while keeping the string short enough that ``int`` on it is
#: never interesting.
POSITIVE_INT = re.compile(r"[1-9][0-9]{0,9}")

#: ISO-8601, as much of it as this appliance's callers write: a date, optionally
#: a time to the second (with a fraction), optionally a zone. Deliberately not a
#: full RFC 3339 parser — the value is handed to PostgreSQL, which has one, and
#: this exists to keep a typo from reaching it rather than to be one.
#:
#: ``routers/logs.py`` builds ``--since`` on top of this, adding a relative-age
#: alternative (``30m``, ``1h``, ``7d``) that journald understands and a
#: timestamp column does not. That is the whole of the difference between the
#: two, and it is why they share this fragment instead of each carrying a regex.
ISO_TIMESTAMP_PATTERN = (
    r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?)?(?:Z|[+-]\d{2}:?\d{2})?"
)
ISO_TIMESTAMP = re.compile(ISO_TIMESTAMP_PATTERN)


def positive_int(value: str, *, field: str, maximum: int | None = None) -> int:
    """``value`` as a positive int, or a flat 422 naming ``field``.

    ``fullmatch`` and not ``match``: ``$`` also matches before a trailing
    newline, so ``match`` would admit ``"5\\n"`` — a value the URL did name and
    the route did not mean.
    """
    if not POSITIVE_INT.fullmatch(str(value)):
        raise HTTPException(status_code=422, detail=f"{field} must be a positive number")
    parsed = int(value)
    if maximum is not None and parsed > maximum:
        raise HTTPException(status_code=422, detail=f"{field} must be between 1 and {maximum}")
    return parsed


def iso_timestamp(value: str, *, field: str) -> str:
    """``value`` as a CANONICAL timestamp string, or a flat 422.

    Parsed and re-emitted, not waved through. Two reasons, and the second is
    why this returns a different string than it was given:

    1. the pattern is a shape, not a calendar. ``2026-13-01`` is four-two-two
       digits and is not a month; ``2026-02-30`` is not a day. Both matched,
       reached PostgreSQL's timestamp comparison, and came back as a 500 — a
       caller's error rendered as an appliance fault, which is exactly what
       validating this was supposed to stop;
    2. what leaves this function is then built by ``datetime.isoformat`` rather
       than by the caller. That matters most for
       ``routers/logs.py``, where the value goes on a COMMAND LINE: a token
       assembled from a parsed ``datetime`` carries nothing of the request
       string, which is a structural guarantee rather than an argument about
       how good the pattern is.

    The pattern still runs first: it is cheap, it bounds what the parser is
    asked to read, and it refuses the spellings ``fromisoformat`` accepts that
    this appliance has no use for.
    """
    text = str(value)
    if not ISO_TIMESTAMP.fullmatch(text):
        raise HTTPException(
            status_code=422, detail=f"{field} must be an ISO-8601 date or timestamp"
        )
    try:
        return datetime.fromisoformat(text).isoformat()
    except ValueError:
        raise HTTPException(
            status_code=422, detail=f"{field} must be an ISO-8601 date or timestamp"
        ) from None
