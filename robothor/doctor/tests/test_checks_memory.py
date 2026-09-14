"""Identity blocks that are read on every turn must not rot silently."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from robothor.doctor.checks import memory as memory_checks
from robothor.doctor.tests.conftest import fake_db, make_ctx

NOW = datetime(2026, 9, 14, 22, 0, tzinfo=UTC)


def _run(ctx):
    check = next(c for c in memory_checks.CHECKS if c.id == "memory.identity_blocks")
    return asyncio.run(check.run(ctx))


def _row(name: str, chars: int, days_ago: float):
    return (name, chars, NOW - timedelta(days=days_ago))


def test_fresh_full_blocks_pass() -> None:
    ctx = make_ctx(db_factory=fake_db([_row("persona", 900, 3), _row("user_profile", 700, 40)]))
    result = _run(ctx)
    assert result.status == "pass"


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
