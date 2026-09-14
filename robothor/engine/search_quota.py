"""The Brave Search quota, known in advance rather than discovered by failing.

Brave's free plan is 2,000 queries per 30 days at one per second, and it
says so on every response::

    x-ratelimit-limit:     1, 2000
    x-ratelimit-policy:    1;w=1, 2000;w=2592000
    x-ratelimit-remaining: 0, 1965
    x-ratelimit-reset:     1, 1398406

Two numbers per header — the per-second view, then the monthly one. The fleet
averages 50-60 searches a day with a 225-search day this month, which is right
at the cap. Without this module a month that runs out looks exactly like a
one-second rate limit: three retries with backoff on every call, then a fall
through to a scraper whose engines block data-center IPs, and nobody told
until the operator asks for a bakery and gets Wikipedia.

So: parse the headers on every reply, refuse to dial a dead month (and say
why in the tool result), and hand ``search_health`` enough to warn before
the month runs out.

Nothing here touches a secret; the key never enters this module.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Below this share of the month, a Brave answer carries its quota and the
#: pager warns. A tenth of 2,000 is 200 queries — three or four days of
#: ordinary use, enough time to hand over a second provider key.
LOW_WATER_FRACTION = 0.1

EXHAUSTED_REASON = "brave_monthly_quota_exhausted"


@dataclass(frozen=True)
class QuotaSnapshot:
    month_limit: int
    month_remaining: int
    #: Absolute epoch seconds at which the monthly window resets.
    month_reset_at: float
    second_remaining: int
    observed_at: float


def _pair(headers: Mapping[str, Any], name: str) -> tuple[int, int] | None:
    """The ``"<per-second>, <monthly>"`` pair a Brave header carries, or None.

    A lone number is NOT read as the monthly figure: the shape is the
    contract, and a single value would silently swap the two views.
    """
    raw = headers.get(name)
    if raw is None:
        return None
    parts = [p.strip() for p in str(raw).split(",")]
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def parse_quota(headers: Mapping[str, Any], now: float) -> QuotaSnapshot | None:
    """Read Brave's rate-limit headers; None when they are absent or malformed."""
    lowered = {str(k).lower(): v for k, v in headers.items()}
    limit = _pair(lowered, "x-ratelimit-limit")
    remaining = _pair(lowered, "x-ratelimit-remaining")
    reset = _pair(lowered, "x-ratelimit-reset")
    if limit is None or remaining is None or reset is None:
        return None
    return QuotaSnapshot(
        month_limit=limit[1],
        month_remaining=remaining[1],
        month_reset_at=now + reset[1],
        second_remaining=remaining[0],
        observed_at=now,
    )


class BraveQuota:
    """What the last Brave reply said about the month, and whether to dial again.

    One instance per process (``QUOTA`` below); the engine is the only process
    that calls Brave, so in-memory is the truth. A restart forgets it, and the
    next reply refills it — the cost of forgetting is one dial, not a page.
    """

    def __init__(self) -> None:
        self.snapshot: QuotaSnapshot | None = None
        self.exhausted_until: float | None = None

    def reset(self) -> None:
        self.snapshot = None
        self.exhausted_until = None

    def record(self, headers: Mapping[str, Any], status: int, now: float | None = None) -> None:
        """Note what a reply said. A 429 with no monthly budget left opens the
        breaker until the window resets; any other reply closes it."""
        at = time.time() if now is None else now
        snap = parse_quota(headers, at)
        if snap is not None:
            self.snapshot = snap
        if status == 429 and snap is not None and snap.month_remaining <= 0:
            self.exhausted_until = snap.month_reset_at
        elif status != 429:
            self.exhausted_until = None

    def monthly_exhausted(self, now: float | None = None) -> bool:
        at = time.time() if now is None else now
        return self.exhausted_until is not None and at < self.exhausted_until

    def skip_reason(self, now: float | None = None) -> str | None:
        """Why Brave should not be dialled right now, or None to go ahead."""
        if self.monthly_exhausted(now):
            return EXHAUSTED_REASON
        return None

    def low_water(self, fraction: float = LOW_WATER_FRACTION) -> bool:
        snap = self.snapshot
        if snap is None or snap.month_limit <= 0:
            return False
        return snap.month_remaining <= snap.month_limit * fraction

    def describe(self, now: float | None = None) -> dict[str, Any] | None:
        """An operator-readable summary, or None before the first reply."""
        snap = self.snapshot
        if snap is None:
            return None
        at = time.time() if now is None else now
        return {
            "provider": "brave",
            "month_remaining": snap.month_remaining,
            "month_limit": snap.month_limit,
            "resets_in_days": round(max(snap.month_reset_at - at, 0.0) / 86400, 1),
            "exhausted": self.monthly_exhausted(at),
        }


#: The engine's one view of the Brave month. ``web.py`` records into it and
#: ``search_health`` reads it.
QUOTA = BraveQuota()
