"""The command line: flags, and compare-only exit codes."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from bench.turn.cli import build_parser, main
from bench.turn.compare import EMPTY_BASELINE

if TYPE_CHECKING:
    from pathlib import Path


def test_help_lists_the_documented_flags(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--help"])
    out = capsys.readouterr().out
    for flag in (
        "--engine-url",
        "--db-url",
        "--runs",
        "--half",
        "--threshold",
        "--baseline",
        "--out",
        "--update-baseline",
        "--compare",
        "--restart-cmd",
        "--token",
    ):
        assert flag in out


def test_defaults() -> None:
    args = build_parser().parse_args([])
    assert args.runs == 20
    assert args.threshold == pytest.approx(0.10)
    assert args.half == "both"
    assert args.engine_url == "http://127.0.0.1:18800"
    # A few ms of scheduler noise on a sub-10 ms metric is not a regression.
    assert args.min_delta == pytest.approx(5.0)


def _write(path: Path, data: dict) -> str:
    path.write_text(json.dumps(data))
    return str(path)


def _result(p50: float) -> dict:
    return {"half": "inprocess", "summary": {"done_ms": {"p50": p50, "p95": p50, "n": 20}}}


def test_compare_only_exits_one_on_a_regression(tmp_path: Path) -> None:
    baseline = _write(tmp_path / "b.json", _result(100.0))
    current = _write(tmp_path / "c.json", _result(111.0))
    assert main(["--compare", current, "--baseline", baseline]) == 1


def test_compare_only_exits_zero_within_threshold(tmp_path: Path) -> None:
    baseline = _write(tmp_path / "b.json", _result(100.0))
    current = _write(tmp_path / "c.json", _result(109.0))
    assert main(["--compare", current, "--baseline", baseline]) == 0


def test_a_named_baseline_that_is_the_empty_sentinel_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Naming a file means comparing against it; an empty one compares nothing.
    baseline = _write(tmp_path / "b.json", EMPTY_BASELINE)
    current = _write(tmp_path / "c.json", _result(9999.0))
    assert main(["--compare", current, "--baseline", baseline]) == 2
    err = capsys.readouterr().err
    assert "empty" in err
    assert str(baseline) in err


def test_a_named_baseline_that_does_not_exist_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    current = _write(tmp_path / "c.json", _result(9999.0))
    missing = tmp_path / "typo.json"
    assert main(["--compare", current, "--baseline", str(missing)]) == 2
    err = capsys.readouterr().err
    assert "not found" in err
    assert str(missing) in err


def _sentinels(folder: Path) -> None:
    folder.mkdir()
    for name in ("inprocess.json", "http.json", "ci-inprocess.json", "ci-http.json"):
        _write(folder / name, EMPTY_BASELINE)


def test_the_default_baseline_as_the_empty_sentinel_is_a_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import bench.turn.cli as cli

    _sentinels(tmp_path / "baselines")
    monkeypatch.setattr(cli, "BASELINES", tmp_path / "baselines")
    current = _write(tmp_path / "c.json", _result(9999.0))
    assert main(["--compare", current]) == 0
    assert "no baseline recorded" in capsys.readouterr().out


def test_the_ci_baseline_set_reads_the_ci_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import bench.turn.cli as cli

    _sentinels(tmp_path / "baselines")
    _write(tmp_path / "baselines" / "ci-inprocess.json", _result(100.0))
    monkeypatch.setattr(cli, "BASELINES", tmp_path / "baselines")
    current = _write(tmp_path / "c.json", _result(200.0))
    assert main(["--compare", current, "--baseline-set", "ci"]) == 1
    assert "ci-inprocess.json" in capsys.readouterr().out
    # The local set is still the empty sentinel: skipped, exit 0.
    assert main(["--compare", current]) == 0


def test_the_ci_baseline_set_skips_an_empty_ci_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bench.turn.cli as cli

    _sentinels(tmp_path / "baselines")
    monkeypatch.setattr(cli, "BASELINES", tmp_path / "baselines")
    current = _write(tmp_path / "c.json", _result(200.0))
    assert main(["--compare", current, "--baseline-set", "ci"]) == 0


def test_update_baseline_from_a_result_writes_it(tmp_path: Path) -> None:
    baseline = tmp_path / "b.json"
    _write(baseline, EMPTY_BASELINE)
    current = _write(tmp_path / "c.json", _result(42.0))
    assert main(["--compare", current, "--baseline", str(baseline), "--update-baseline"]) == 0
    written = json.loads(baseline.read_text())
    assert written["summary"]["done_ms"]["p50"] == 42.0
    assert main(["--compare", current, "--baseline", str(baseline)]) == 0


def _driven(status: str) -> dict:
    return {
        **_result(10.0),
        "runs": 2,
        "turns": [{"done_ms": 10.0}, {"done_ms": 10.0}],
        "meta": [{"turn": 0, "status": "completed"}, {"turn": 1, "status": status}],
        "notes": {},
    }


def test_a_failed_turn_fails_the_benchmark_and_writes_no_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bench.turn.cli as cli

    monkeypatch.setattr(cli, "_drive", lambda args, half, db_url: _driven("failed"))
    baseline = tmp_path / "b.json"
    _write(baseline, EMPTY_BASELINE)
    code = main(["--half", "inprocess", "--baseline", str(baseline), "--update-baseline"])
    assert code == 2
    assert json.loads(baseline.read_text()) == EMPTY_BASELINE


def test_completed_turns_record_a_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import bench.turn.cli as cli

    monkeypatch.setattr(cli, "_drive", lambda args, half, db_url: _driven("completed"))
    baseline = tmp_path / "b.json"
    code = main(["--half", "inprocess", "--baseline", str(baseline), "--update-baseline"])
    assert code == 0
    assert json.loads(baseline.read_text())["summary"]["done_ms"]["p50"] == 10.0


def test_warmup_turns_are_dropped_from_the_summary_and_kept_apart() -> None:
    from bench.turn.cli import split_warmup

    turns = [{"done_ms": 500.0}, {"done_ms": 40.0}, {"done_ms": 42.0}]
    meta = [{"turn": 0}, {"turn": 1}, {"turn": 2}]
    kept, kept_meta, warm = split_warmup(turns, meta, 1)
    assert kept == [{"done_ms": 40.0}, {"done_ms": 42.0}]
    assert [m["turn"] for m in kept_meta] == [1, 2]
    assert warm == [{"done_ms": 500.0}]


def test_warmup_defaults_to_one_turn() -> None:
    assert build_parser().parse_args([]).warmup == 1


def _both_sets(folder: Path, value: float) -> None:
    _sentinels(folder)
    _write(folder / "inprocess.json", _result(value))
    _write(folder / "ci-inprocess.json", _result(value))


def test_the_ci_set_defaults_timing_metrics_to_thirty_percent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bench.turn.cli as cli

    _both_sets(tmp_path / "baselines", 100.0)
    monkeypatch.setattr(cli, "BASELINES", tmp_path / "baselines")
    current = _write(tmp_path / "c.json", _result(125.0))
    assert main(["--compare", current, "--baseline-set", "ci"]) == 0
    assert main(["--compare", current]) == 1  # local: 10%


def test_an_explicit_timing_threshold_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import bench.turn.cli as cli

    _both_sets(tmp_path / "baselines", 100.0)
    monkeypatch.setattr(cli, "BASELINES", tmp_path / "baselines")
    current = _write(tmp_path / "c.json", _result(125.0))
    assert main(["--compare", current, "--baseline-set", "ci", "--timing-threshold", "0.2"]) == 1
    assert main(["--compare", current, "--timing-threshold", "0.3"]) == 0


def _summary_file(path: Path, half: str, metrics: dict[str, float]) -> str:
    return _write(
        path,
        {
            "half": half,
            "runs": 20,
            "summary": {m: {"p50": v, "p95": v * 2, "n": 20} for m, v in metrics.items()},
        },
    )


def test_merge_baselines_takes_the_median_per_metric(tmp_path: Path) -> None:
    a = _summary_file(tmp_path / "a.json", "inprocess", {"done_ms": 10.0, "db_statements": 64.0})
    b = _summary_file(tmp_path / "b.json", "inprocess", {"done_ms": 30.0, "db_statements": 64.0})
    c = _summary_file(tmp_path / "c.json", "inprocess", {"done_ms": 20.0})
    out = tmp_path / "merged.json"
    assert main(["--merge-baselines", a, b, c, "--out", str(out)]) == 0
    merged = json.loads(out.read_text())
    assert merged["half"] == "inprocess"
    assert merged["summary"]["done_ms"]["p50"] == 20.0
    assert merged["summary"]["done_ms"]["p95"] == 40.0
    assert merged["summary"]["db_statements"]["p50"] == 64.0
    assert len(merged["merged_from"]) == 3
    # The merged file is a usable baseline.
    current = _write(tmp_path / "now.json", _result(20.0))
    assert main(["--compare", current, "--baseline", str(out)]) == 0


def test_merge_baselines_needs_three_runs(tmp_path: Path) -> None:
    a = _summary_file(tmp_path / "a.json", "inprocess", {"done_ms": 10.0})
    b = _summary_file(tmp_path / "b.json", "inprocess", {"done_ms": 30.0})
    assert main(["--merge-baselines", a, b, "--out", str(tmp_path / "m.json")]) == 2
    assert not (tmp_path / "m.json").exists()


def test_merge_baselines_refuses_mixed_halves(tmp_path: Path) -> None:
    files = [
        _summary_file(tmp_path / f"{n}.json", half, {"done_ms": 10.0})
        for n, half in (("a", "inprocess"), ("b", "inprocess"), ("c", "http"))
    ]
    assert main(["--merge-baselines", *files, "--out", str(tmp_path / "m.json")]) == 2


def test_merge_baselines_needs_out(tmp_path: Path) -> None:
    files = [_summary_file(tmp_path / f"{n}.json", "inprocess", {"done_ms": 1.0}) for n in "abc"]
    assert main(["--merge-baselines", *files]) == 2
