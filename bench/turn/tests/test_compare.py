"""A run's summary against the committed baseline."""

from __future__ import annotations

import json
from pathlib import Path

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
