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


def test_compare_against_the_empty_sentinel_is_a_no_op(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = _write(tmp_path / "b.json", EMPTY_BASELINE)
    current = _write(tmp_path / "c.json", _result(9999.0))
    assert main(["--compare", current, "--baseline", baseline]) == 0
    assert "no baseline recorded" in capsys.readouterr().out


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
