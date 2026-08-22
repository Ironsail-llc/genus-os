"""103 splits ``benchmark_results.pass_rate`` from the partial-credit aggregate.

``pass_rate`` has stored the weighted partial-credit aggregate since 063 while
``passed``/``failed`` counted cases against a 0.70 threshold. On the live table
1956 of 2367 rows disagreed with their own ``passed/total_cases``.

103 must do two things, not one: move the historical number into
``aggregate_score`` (where it is true) *and* recompute ``pass_rate`` from the
per-row ``passed``/``total_cases`` that every row already carries. Relabelling
alone would leave history wrong; both inputs exist, so history becomes correct.

Pure file-parse test (no DB), test_migration_098 style.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MIGRATION = REPO / "crm" / "migrations" / "103_benchmark_pass_rate_truth.sql"
MANIFEST = REPO / "robothor" / "migrations" / "manifest.txt"


def _sql() -> str:
    return MIGRATION.read_text()


def test_migration_exists() -> None:
    assert MIGRATION.exists(), f"missing {MIGRATION}"


def test_migration_is_in_the_manifest() -> None:
    """Only manifest entries are packaged and discovered by the runner."""
    entries = [
        line.strip()
        for line in MANIFEST.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert f"crm/{MIGRATION.name}" in entries


def test_adds_aggregate_score_column() -> None:
    sql = _sql().lower()
    assert re.search(
        r"alter\s+table\s+benchmark_results\s+add\s+column\s+if\s+not\s+exists\s+aggregate_score",
        sql,
    ), "aggregate_score must be added idempotently"


def test_adds_judge_errors_column() -> None:
    """A rate-limited judge has to be visible in the table, not just in a log."""
    sql = _sql().lower()
    assert re.search(
        r"alter\s+table\s+benchmark_results\s+add\s+column\s+if\s+not\s+exists\s+judge_errors",
        sql,
    )


def test_backfills_aggregate_score_from_pass_rate() -> None:
    """The historical pass_rate value IS the partial-credit aggregate."""
    sql = _sql().lower()
    assert re.search(
        r"update\s+benchmark_results\s+set\s+aggregate_score\s*=\s*pass_rate",
        sql,
    ), "aggregate_score must inherit the historical pass_rate value"
    assert "aggregate_score is null" in sql, "backfill must be re-runnable"


def test_recomputes_historical_pass_rate_from_counts() -> None:
    """History becomes correct, not merely relabelled."""
    sql = _sql().lower()
    assert re.search(
        r"set\s+pass_rate\s*=\s*passed(::real|::numeric)?\s*/\s*total_cases",
        sql,
    ), "pass_rate must be recomputed as passed / total_cases"


def test_recompute_runs_after_the_backfill() -> None:
    """Recomputing first would destroy the aggregate before it is copied."""
    sql = _sql().lower()
    backfill = sql.index("set aggregate_score = pass_rate")
    recompute = sql.index("set pass_rate = passed")
    assert backfill < recompute


def test_zero_case_rows_are_not_divided_by_zero() -> None:
    sql = _sql().lower()
    assert "total_cases > 0" in sql


def test_documents_why_history_is_recomputed() -> None:
    """The comment is the only place the relabel-vs-recompute choice is recorded."""
    sql = _sql().lower()
    assert "aggregate" in sql
    assert "recompute" in sql or "recomputed" in sql
