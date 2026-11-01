"""p50/p95 per metric, ignoring turns that did not record it."""

from __future__ import annotations

import pytest

from bench.turn.stats import percentile, summarize


def test_percentiles_interpolate_linearly() -> None:
    values = [float(v) for v in range(1, 21)]
    assert percentile(values, 50) == pytest.approx(10.5)
    assert percentile(values, 95) == pytest.approx(19.05)


def test_percentile_order_does_not_matter() -> None:
    assert percentile([3.0, 1.0, 2.0], 50) == pytest.approx(2.0)


def test_percentile_of_one_value_is_that_value() -> None:
    assert percentile([7.0], 95) == pytest.approx(7.0)


def test_percentile_of_nothing_is_none() -> None:
    assert percentile([], 50) is None


def test_summarize_skips_missing_values_and_counts_the_rest() -> None:
    turns: list[dict[str, float | None]] = [
        {"done_ms": 10.0, "ttft_ms": None},
        {"done_ms": 20.0, "ttft_ms": 5.0},
        {"done_ms": 30.0},
    ]
    summary = summarize(turns)
    assert summary["done_ms"] == {"p50": pytest.approx(20.0), "p95": pytest.approx(29.0), "n": 3}
    assert summary["ttft_ms"]["n"] == 1
    assert summary["ttft_ms"]["p50"] == pytest.approx(5.0)


def test_summarize_leaves_out_a_metric_no_turn_recorded() -> None:
    assert "db_statements" not in summarize([{"done_ms": 1.0, "db_statements": None}])
