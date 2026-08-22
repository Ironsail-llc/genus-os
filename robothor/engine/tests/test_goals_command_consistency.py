"""The two consumers of ``benchmark_results`` must agree with the table.

Two defects are pinned here:

1. The Telegram ``/goals`` command printed ``{passed}/{total}`` from one pair
   of columns and ``({pct}%)`` from ``pass_rate`` — which held the weighted
   partial-credit aggregate. crm-hygiene rendered as ``0/4 (18%)``.
2. Neither ``/goals`` nor ``goals.py::_get_benchmark_pass_rate`` filtered by
   suite, so ``DISTINCT ON (agent_id)`` handed an agent's grade to whichever
   suite wrote last. That is exactly how 709 synthetic rows under suites
   ``s1``/``s2``/``test-suite`` became agent ``main``'s score.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from robothor.engine.goals import _get_benchmark_pass_rate


class _FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.last_sql: str | None = None
        self.last_params: Any = None

    def execute(self, sql: str, params: Any = None) -> _FakeCursor:
        self.last_sql = sql
        self.last_params = params
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _FakeConn:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.cursors: list[_FakeCursor] = []

    def cursor(self) -> _FakeCursor:
        cur = _FakeCursor(self.rows)
        self.cursors.append(cur)
        return cur

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class TestPassRateSuiteFilter:
    def test_suite_id_is_pushed_into_the_query(self):
        """An agent's grade comes from its own suite, not whatever wrote last."""
        conn = _FakeConn([(3, 4)])
        with patch("robothor.crm.dal.get_connection", return_value=conn):
            value = _get_benchmark_pass_rate("main", window_days=7, suite_id="main-harness")

        assert value == pytest.approx(0.75)
        cur = conn.cursors[0]
        assert cur.last_sql is not None
        assert "suite_id" in cur.last_sql
        assert "main-harness" in tuple(cur.last_params)

    def test_no_suite_filter_when_suite_id_is_unknown(self):
        """Without a canonical suite the query stays as it was — no silent drop."""
        conn = _FakeConn([(3, 4)])
        with patch("robothor.crm.dal.get_connection", return_value=conn):
            value = _get_benchmark_pass_rate("main", window_days=7)

        assert value == pytest.approx(0.75)
        cur = conn.cursors[0]
        assert cur.last_sql is not None
        assert "suite_id = " not in cur.last_sql


class TestCanonicalSuiteId:
    def test_reads_the_id_key(self, tmp_path):
        from robothor.engine.tools.handlers.benchmark import canonical_suite_id

        suite = tmp_path / "docs" / "benchmarks" / "main"
        suite.mkdir(parents=True)
        (suite / "suite.yaml").write_text("id: main-harness\ntasks: []\n")

        assert canonical_suite_id("main", str(tmp_path)) == "main-harness"

    def test_accepts_the_suite_id_key(self, tmp_path):
        from robothor.engine.tools.handlers.benchmark import canonical_suite_id

        suite = tmp_path / "docs" / "benchmarks" / "crm-dedup"
        suite.mkdir(parents=True)
        (suite / "suite.yaml").write_text("suite_id: crm-dedup-v1\ntasks: []\n")

        assert canonical_suite_id("crm-dedup", str(tmp_path)) == "crm-dedup-v1"

    def test_returns_none_when_there_is_no_suite(self, tmp_path):
        from robothor.engine.tools.handlers.benchmark import canonical_suite_id

        assert canonical_suite_id("ghost", str(tmp_path)) is None


class TestFormatAgentGrades:
    def _grade(self, **over: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            "agent_id": "crm-hygiene",
            "suite_id": "crm-hygiene-harness",
            "total_cases": 4,
            "passed": 0,
            "aggregate_score": 0.1818,
            "judge_errors": 0,
            "failing_case_ids": ["stale-todo", "bad-phone"],
        }
        row.update(over)
        return row

    def test_percentage_matches_the_fraction(self):
        from robothor.engine.telegram import format_agent_grades

        text = format_agent_grades([self._grade()])

        assert "0/4 (0%)" in text
        assert "(18%)" not in text

    def test_partial_credit_is_labelled_not_disguised(self):
        from robothor.engine.telegram import format_agent_grades

        text = format_agent_grades([self._grade()])

        # The aggregate may still be shown, but never as the pass rate.
        assert "partial credit" in text.lower()

    def test_judge_errors_are_surfaced(self):
        from robothor.engine.telegram import format_agent_grades

        text = format_agent_grades([self._grade(judge_errors=2)])

        assert "judge error" in text.lower()

    def test_worst_agent_comes_first(self):
        from robothor.engine.telegram import format_agent_grades

        text = format_agent_grades(
            [
                self._grade(agent_id="good", passed=4, total_cases=4),
                self._grade(agent_id="bad", passed=0, total_cases=4),
            ]
        )
        assert text.index("bad") < text.index("good")

    def test_zero_case_row_does_not_divide_by_zero(self):
        from robothor.engine.telegram import format_agent_grades

        text = format_agent_grades([self._grade(total_cases=0, passed=0)])

        assert "0/0" in text
