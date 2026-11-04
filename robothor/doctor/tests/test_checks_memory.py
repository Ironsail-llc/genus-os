"""Identity blocks that are read on every turn must not rot silently."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from robothor.doctor.checks import memory as memory_checks
from robothor.doctor.tests.conftest import fake_db, make_ctx

NOW = datetime(2026, 9, 14, 22, 0, tzinfo=UTC)


def _run(ctx, now: datetime | None = None):
    check = next(c for c in memory_checks.CHECKS if c.id == "memory.identity_blocks")
    if now is None:
        return asyncio.run(check.run(ctx))
    # The registered check reads the real clock; a test pins its own.
    return asyncio.run(memory_checks._identity_blocks(ctx, now=now))


def _row(name: str, chars: int, days_ago: float, now: datetime = NOW):
    return (name, chars, now - timedelta(days=days_ago))


def test_fresh_full_blocks_pass() -> None:
    ctx = make_ctx(db_factory=fake_db([_row("persona", 900, 3), _row("user_profile", 700, 40)]))
    result = _run(ctx, now=NOW)
    assert result.status == "pass"


def test_fresh_blocks_pass_against_a_pinned_clock_whatever_the_real_date() -> None:
    # Pinned years before the real date: a check that read the wall clock
    # would see these blocks as years old.
    pinned = datetime(2020, 1, 1, tzinfo=UTC)
    rows = [_row("persona", 900, 3, pinned), _row("user_profile", 700, 40, pinned)]
    result = _run(make_ctx(db_factory=fake_db(rows)), now=pinned)
    assert result.status == "pass", result.detail


def test_a_block_older_than_ninety_days_on_the_pinned_clock_is_flagged() -> None:
    # Pinned far after the real date: a check that read the wall clock would
    # see this block as written in the future, and pass it.
    pinned = datetime(2099, 1, 1, tzinfo=UTC)
    rows = [_row("persona", 900, 120, pinned), _row("user_profile", 700, 10, pinned)]
    result = _run(make_ctx(db_factory=fake_db(rows)), now=pinned)
    assert result.status == "fail"
    assert result.detail.startswith("persona: last written 120 days ago")


def test_a_block_older_than_ninety_days_fails_and_names_it() -> None:
    rows = [_row("persona", 360, 215), _row("user_profile", 700, 10)]
    problems = memory_checks._judge(
        [{"block": r[0], "chars": r[1], "written": r[2]} for r in rows], NOW
    )
    assert len(problems) == 1
    assert problems[0].startswith("persona: last written 215 days ago")
    assert "live engine state" in problems[0]


def test_a_placeholder_sized_block_fails_even_when_recent() -> None:
    problems = memory_checks._judge(
        [
            {"block": "persona", "chars": 40, "written": NOW},
            {"block": "user_profile", "chars": 700, "written": NOW},
        ],
        NOW,
    )
    assert problems == ["persona: 40 chars — a placeholder, write the real block"]


def test_a_missing_block_fails() -> None:
    problems = memory_checks._judge([{"block": "persona", "chars": 700, "written": NOW}], NOW)
    assert problems == ["user_profile: missing — write it (memory_block_write)"]


def test_the_live_shape_on_2026_09_14_is_reported_as_two_problems() -> None:
    # persona 360 chars / 215 days; user_profile 345 chars / 156 days.
    problems = memory_checks._judge(
        [
            {"block": "persona", "chars": 360, "written": NOW - timedelta(days=215)},
            {"block": "user_profile", "chars": 345, "written": NOW - timedelta(days=156)},
        ],
        NOW,
    )
    assert len(problems) == 2


def test_no_database_is_a_skip_not_a_pass() -> None:
    ctx = make_ctx(db_factory=fake_db(error=RuntimeError("no database")))
    result = _run(ctx)
    assert result.status == "skip"
    assert "RuntimeError" in result.detail


def test_the_check_is_recommended_and_not_fixable() -> None:
    check = memory_checks.CHECKS[0]
    assert check.severity == "recommended"
    assert check.fix is None
