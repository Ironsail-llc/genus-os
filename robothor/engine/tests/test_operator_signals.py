"""Tests for operator signals (self-improvement Phase 2)."""

from __future__ import annotations

from datetime import UTC, datetime

from robothor.engine.operator_signals import (
    operator_verdict_for_run,
    reaction_to_verdict,
)


class TestReactionToVerdict:
    def test_strong_positive(self):
        assert reaction_to_verdict("👍") == 2
        assert reaction_to_verdict("🔥") == 2

    def test_strong_negative(self):
        assert reaction_to_verdict("😡") == -2
        assert reaction_to_verdict("👎") == -2

    def test_weak(self):
        assert reaction_to_verdict("👌") == 1
        assert reaction_to_verdict("🙄") == -1

    def test_unknown_and_empty_are_neutral(self):
        assert reaction_to_verdict("🦄") == 0
        assert reaction_to_verdict("") == 0
        assert reaction_to_verdict(None) == 0


class _Cur:
    """Cursor returning queued fetchone results per execute, in order."""

    def __init__(self, results):
        self._results = list(results)
        self._last = None

    def execute(self, sql, params=None):
        self._last = self._results.pop(0) if self._results else None

    def fetchone(self):
        return self._last


class TestOperatorVerdictForRun:
    def _t(self):
        return datetime(2026, 5, 30, tzinfo=UTC), datetime(2026, 5, 30, 23, 59, tzinfo=UTC)

    def test_run_linked_reaction_wins(self):
        start, end = self._t()
        # (1) run reaction = -2, (2) intervention count = 0
        cur = _Cur([(-2,), (0,)])
        assert operator_verdict_for_run(cur, "run-1", "main", start, end) == -2

    def test_intervention_is_strong_negative(self):
        start, end = self._t()
        # (1) no run reaction, (2) one intervention
        cur = _Cur([(None,), (1,)])
        assert operator_verdict_for_run(cur, "run-1", "main", start, end) == -1

    def test_most_negative_of_reaction_and_intervention(self):
        start, end = self._t()
        cur = _Cur([(-2,), (1,)])  # reaction -2 beats intervention -1
        assert operator_verdict_for_run(cur, "run-1", "main", start, end) == -2

    def test_falls_back_to_window_mood(self):
        start, end = self._t()
        # (1) no run reaction, (2) no intervention, (3) window worst = -1
        cur = _Cur([(None,), (0,), (-1,)])
        assert operator_verdict_for_run(cur, "run-1", "main", start, end) == -1

    def test_none_when_no_signal(self):
        start, end = self._t()
        cur = _Cur([(None,), (0,), (None,)])
        assert operator_verdict_for_run(cur, "run-1", "main", start, end) is None
