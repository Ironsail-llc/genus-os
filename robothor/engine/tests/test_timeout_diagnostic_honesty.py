"""A timeout must report what actually happened, not what was configured.

`fleet-analysis` was killed at 119.96s by an outer per-tool cap and filed as
"harness timeout after 1800s" — the suite's configured budget, which had not
been reached and was not the thing that fired. That message is why raising the
budget in #327 looked like it should have fixed the case and did not: it named
the wrong layer, so the next reader looked in the wrong place.

A diagnostic that reports a number nobody measured is worse than no number.
"""

from __future__ import annotations

from robothor.engine.tools.handlers.benchmark import _timeout_result

_TASK = {"id": "fleet-analysis", "category": "correctness", "weight": 1.0}


def test_reports_the_elapsed_time_not_the_budget() -> None:
    r = _timeout_result(_TASK, 1800.0, "agent-architect", "suite", elapsed_seconds=119.96)
    assert "120s" in r["reason"]
    assert "1800s" in r["reason"], "the budget belongs in the message as context"
    assert r["elapsed_seconds"] == 120.0


def test_says_plainly_that_a_smaller_cap_fired_first() -> None:
    """The reader must not have to infer it from two numbers."""
    r = _timeout_result(_TASK, 1800.0, "agent-architect", "suite", elapsed_seconds=119.96)
    assert "smaller cap fired first" in r["reason"]


def test_a_genuine_budget_overrun_reads_plainly() -> None:
    """When the configured cap IS what fired, say so without the extra clause."""
    r = _timeout_result(_TASK, 900.0, "agent-architect", "suite", elapsed_seconds=900.4)
    assert "smaller cap" not in r["reason"]
    assert "900s" in r["reason"]


def test_missing_elapsed_falls_back_to_the_cap() -> None:
    """Older call sites pass no elapsed; they must not report a wrong number."""
    r = _timeout_result(_TASK, 900.0, "agent-architect", "suite")
    assert "900s" in r["reason"]
    assert "smaller cap" not in r["reason"]


def test_a_timeout_is_never_a_grade() -> None:
    """The existing contract still holds: hard 0.0, labelled, counted."""
    r = _timeout_result(_TASK, 1800.0, "agent-architect", "suite", elapsed_seconds=119.96)
    assert r["score"] == 0.0
    assert r["timed_out"] is True
    assert r["output_preview"] == ""


def test_a_non_numeric_elapsed_is_ignored_not_arithmetic() -> None:
    """Callers pass a duration straight off the run record.

    That is None on a run that never started and a mock under test; doing
    arithmetic on it raised inside the timeout path, turning a timeout into an
    'error' outcome and losing the timeout accounting entirely.
    """
    for bogus in (None, object(), "900"):
        r = _timeout_result(_TASK, 900.0, "a", "s", elapsed_seconds=bogus)  # type: ignore[arg-type]
        assert r["timed_out"] is True
        assert "900s" in r["reason"]
