"""``genus doctor`` lists the budget stops of the last day.

A budget that stopped or would have stopped a run is recorded as a
``budget_stop`` step. This check is where an operator who did not open the
Runs view finds out it happened: an info line, never a verdict on the box.
"""

from __future__ import annotations

import asyncio

from robothor.doctor.checks import budget_stops as checks
from robothor.doctor.tests.conftest import fake_db, make_ctx


def _run(ctx):
    check = next(item for item in checks.CHECKS if item.id == "budget_stops.recent")
    answer = asyncio.run(check.run(ctx))
    return answer if isinstance(answer, list) else [answer]


def test_it_is_registered_as_an_info_check():
    from robothor.doctor.registry import builtin_ids

    assert "budget_stops.recent" in builtin_ids()
    check = next(item for item in checks.CHECKS if item.id == "budget_stops.recent")
    assert check.severity == "info"


def test_it_lists_the_last_24_hours_grouped_by_stop_and_mode():
    db = fake_db([("hourly_cost_cap", "observe", 2), ("durable_stop", "enforce", 1)])
    [result] = _run(make_ctx(db_factory=db))

    assert result.status == "pass"
    assert "3 budget stop" in result.detail
    assert "hourly_cost_cap (observe): 2" in result.detail
    assert "durable_stop (enforce): 1" in result.detail
    sql, _params = db.connection.cursors[0].executed[0]
    assert "budget_stop" in sql
    assert "24 hours" in sql
    # The retention index is on created_at; started_at would scan every step.
    assert "created_at >=" in sql
    assert "started_at" not in sql


def test_none_is_a_pass_that_says_zero():
    [result] = _run(make_ctx(db_factory=fake_db([])))
    assert result.status == "pass"
    assert "0 budget stops" in result.detail


def test_an_unreachable_database_is_a_skip():
    [result] = _run(make_ctx(db_factory=fake_db(error=ConnectionError("refused"))))
    assert result.status == "skip"
