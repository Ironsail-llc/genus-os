"""A run's summary against the committed baseline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.turn.compare import EMPTY_BASELINE, compare, is_empty_baseline


def _result(p50: float, p95: float | None = None, metric: str = "done_ms") -> dict:
    return {"summary": {metric: {"p50": p50, "p95": p95 if p95 is not None else p50, "n": 20}}}


def test_over_the_threshold_is_a_regression() -> None:
    outcome = compare(_result(100.0), _result(111.0), threshold=0.10)
    assert outcome.regressed
    assert [r.metric for r in outcome.regressions] == ["done_ms", "done_ms"]
    assert {r.stat for r in outcome.regressions} == {"p50", "p95"}
    assert outcome.exit_code == 1


def test_within_the_threshold_passes() -> None:
    outcome = compare(_result(100.0), _result(109.0), threshold=0.10)
    assert not outcome.regressed
    assert outcome.exit_code == 0


def test_faster_is_never_a_regression() -> None:
    assert compare(_result(100.0), _result(50.0)).exit_code == 0


def test_the_threshold_is_configurable() -> None:
    assert compare(_result(100.0), _result(111.0), threshold=0.20).exit_code == 0


def test_only_p95_over_the_threshold_still_regresses() -> None:
    outcome = compare(_result(100.0, 100.0), _result(100.0, 150.0))
    assert [(r.metric, r.stat) for r in outcome.regressions] == [("done_ms", "p95")]


def test_a_metric_missing_on_either_side_is_skipped() -> None:
    baseline = {"summary": {"done_ms": {"p50": 1.0, "p95": 1.0, "n": 1}}}
    current = {"summary": {"ttft_ms": {"p50": 9.0, "p95": 9.0, "n": 1}}}
    outcome = compare(baseline, current)
    assert outcome.exit_code == 0
    assert set(outcome.skipped) == {"done_ms", "ttft_ms"}


def test_a_change_below_the_absolute_floor_is_noise() -> None:
    # 0 -> 0.5 is an infinite ratio and still nothing.
    assert compare(_result(0.0), _result(0.5), min_delta=1.0).exit_code == 0


def test_the_empty_sentinel_makes_comparison_a_no_op() -> None:
    outcome = compare(EMPTY_BASELINE, _result(1_000_000.0))
    assert outcome.exit_code == 0
    assert outcome.no_baseline
    assert "no baseline recorded" in outcome.message
    assert "--update-baseline" in outcome.message


def test_the_committed_baselines_are_valid_json_and_empty_until_qa_records_one() -> None:
    folder = Path(__file__).resolve().parent.parent / "baselines"
    files = sorted(folder.glob("*.json"))
    assert {f.name for f in files} >= {"http.json", "inprocess.json", "ci-inprocess.json"}
    for path in files:
        data = json.loads(path.read_text())
        assert "summary" in data
        if is_empty_baseline(data):
            assert compare(data, _result(5.0)).no_baseline


def test_the_report_names_each_metric() -> None:
    outcome = compare(_result(100.0), _result(200.0))
    assert "done_ms" in outcome.message
    assert "p50" in outcome.message


def _metric(metric: str, value: float) -> dict:
    return {"summary": {metric: {"p50": value, "p95": value, "n": 20}}}


def test_timing_metrics_use_the_timing_threshold() -> None:
    assert (
        compare(
            _metric("done_ms", 100.0), _metric("done_ms", 125.0), timing_threshold=0.30
        ).exit_code
        == 0
    )
    assert (
        compare(
            _metric("done_ms", 100.0), _metric("done_ms", 131.0), timing_threshold=0.30
        ).exit_code
        == 1
    )


def test_the_timing_threshold_defaults_to_the_threshold() -> None:
    assert compare(_metric("ttft_ms", 100.0), _metric("ttft_ms", 111.0)).exit_code == 1


@pytest.mark.parametrize("metric", ["db_statements", "prompt_tokens", "tool_schemas"])
def test_count_metrics_keep_the_tight_threshold(metric: str) -> None:
    outcome = compare(_metric(metric, 100.0), _metric(metric, 115.0), timing_threshold=0.30)
    assert outcome.exit_code == 1


@pytest.mark.parametrize("metric", ["db_statements", "prompt_tokens", "tool_schemas"])
def test_count_metrics_ignore_the_min_delta(metric: str) -> None:
    # 10 -> 12 is 20% but only 2 over: still a regression for a count.
    assert compare(_metric(metric, 10.0), _metric(metric, 12.0), min_delta=5.0).exit_code == 1


def test_timing_metrics_keep_the_min_delta() -> None:
    assert compare(_metric("setup_ms", 4.0), _metric("setup_ms", 8.0), min_delta=5.0).exit_code == 0


@pytest.mark.parametrize("metric", ["prompt_tokens", "tool_schemas"])
def test_exact_metrics_regress_on_any_increase(metric: str) -> None:
    outcome = compare(_metric(metric, 216.0), _metric(metric, 217.0), threshold=0.10)
    assert outcome.exit_code == 1
    assert [r.metric for r in outcome.regressions] == [metric, metric]


@pytest.mark.parametrize("metric", ["prompt_tokens", "tool_schemas"])
def test_an_exact_metric_that_drops_passes_and_says_to_re_record(metric: str) -> None:
    outcome = compare(_metric(metric, 216.0), _metric(metric, 200.0))
    assert outcome.exit_code == 0
    assert "re-record" in outcome.message
    assert metric in outcome.message


def test_db_statements_keeps_ten_percent() -> None:
    assert compare(_metric("db_statements", 64.0), _metric("db_statements", 65.0)).exit_code == 0
    assert compare(_metric("db_statements", 64.0), _metric("db_statements", 71.0)).exit_code == 1


def test_an_unchanged_exact_metric_gives_no_hint() -> None:
    assert (
        "re-record"
        not in compare(_metric("tool_schemas", 216.0), _metric("tool_schemas", 216.0)).message
    )
