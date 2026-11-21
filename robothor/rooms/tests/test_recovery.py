"""Which ``building`` editions are dead, decided without a database.

The database half (failing them, per tenant and room) is in
``test_period_requests.py``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from robothor.rooms import recovery
from robothor.rooms.models import Edition

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
EDITION_ID = "0d1c7a3e-8a4b-4f0e-9c55-6a3d7c1e2f90"


def _edition(age: timedelta) -> Edition:
    return Edition(
        tenant_id="test-tenant",
        id=EDITION_ID,
        room_id="7a0c0e8e-63c4-4c5b-9a43-0d6f6f0c2b11",
        seq=1,
        kind="edition",
        status="building",
        preview=False,
        period_start=date(2026, 11, 20),
        period_end=date(2026, 11, 20),
        grain="day",
        revision=1,
        created_at=NOW - age,
    )


OLD = timedelta(seconds=recovery.STALE_BUILDING_SECONDS + 60)
RECENT = timedelta(minutes=5)


@pytest.mark.parametrize(
    ("age", "run_status", "booted_ago", "live", "dead"),
    [
        (RECENT, None, None, False, False),
        (OLD, None, None, False, True),
        (RECENT, None, timedelta(minutes=1), False, True),  # created before the boot
        (RECENT, None, timedelta(minutes=10), False, False),  # created after it
        (OLD, "running", None, False, False),
        (OLD, "awaiting_approval", None, False, False),
        (RECENT, "completed", None, False, True),
        (RECENT, "cancelled", timedelta(hours=1), False, True),
        (OLD, None, timedelta(minutes=1), True, False),  # live here wins
        (OLD, "failed", None, True, False),
    ],
)
def test_what_counts_as_dead(age, run_status, booted_ago, live, dead):
    booted_at = NOW - booted_ago if booted_ago is not None else None
    live_ids = frozenset({EDITION_ID}) if live else frozenset()

    reason = recovery._why_dead(
        _edition(age), run_status, booted_at=booted_at, now=NOW, live_ids=live_ids
    )

    assert (reason is not None) == dead
    if run_status is not None and dead:
        assert run_status in reason


def test_holds_count_and_release_once():
    first, second = recovery.hold(EDITION_ID), recovery.hold(EDITION_ID)

    first()
    first()  # a second release of the same hold changes nothing
    assert EDITION_ID in recovery.live_edition_ids()
    second()
    assert EDITION_ID not in recovery.live_edition_ids()


def test_live_holds_for_a_block_even_when_it_raises():
    with pytest.raises(RuntimeError), recovery.live(EDITION_ID):
        assert EDITION_ID in recovery.live_edition_ids()
        raise RuntimeError("boom")

    assert EDITION_ID not in recovery.live_edition_ids()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("not a time", None),
        ("2026-11-21T10:00:00+00:00", datetime(2026, 11, 21, 10, 0, tzinfo=UTC)),
        ("2026-11-21T10:00:00", datetime(2026, 11, 21, 10, 0, tzinfo=UTC)),  # naive is UTC
    ],
)
def test_the_boot_instant_comes_from_the_daemon(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("ROBOTHOR_DAEMON_START_TS", raising=False)
    else:
        monkeypatch.setenv("ROBOTHOR_DAEMON_START_TS", raw)

    assert recovery._boot() == expected
