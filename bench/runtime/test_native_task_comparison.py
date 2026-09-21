"""A timed-out baseline remains visible and cannot produce a passing comparison."""

import json
import subprocess
from unittest.mock import Mock

import pytest

from bench.runtime import native_task_comparison


def test_timeout_keeps_partial_output_and_still_measures_current(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    worker = tmp_path / "bench/runtime/native_task_comparison_worker.py"
    worker.parent.mkdir(parents=True)
    worker.write_text("# synthetic worker")
    monkeypatch.setenv("ROBOTHOR_RUNTIME_BASELINE_CHECKOUT", str(tmp_path))
    monkeypatch.setattr(
        subprocess, "check_output", lambda *a, **k: "eea3252b1577e3f51e6d6a6216d6f242fd7ac4a1"
    )
    sample = {
        "scenario": "standalone",
        "index": 0,
        "warmup": True,
        "duration_ms": 1,
        "verified": True,
    }
    partial = ("NATIVE_TASK_SAMPLE " + json.dumps(sample) + "\n").encode()
    rows = [
        {**sample, "scenario": case, "index": i, "warmup": i == 0}
        for case in ["standalone", "compound"]
        for i in range(31)
    ]
    completed = subprocess.CompletedProcess(
        [], 0, stdout="\n".join("NATIVE_TASK_SAMPLE " + json.dumps(r) for r in rows), stderr=""
    )
    run = Mock(
        side_effect=[
            subprocess.TimeoutExpired("worker", 240, output=partial, stderr=b"diagnostic"),
            completed,
        ]
    )
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(AssertionError, match="retained per-revision failures"):
        native_task_comparison.drill(tmp_path, {"ROBOTHOR_TEST_DB_DSN": "synthetic"})
    lines = capsys.readouterr().out.splitlines()
    report = json.loads(
        next(s.split(" ", 1)[1] for s in lines if s.startswith("NATIVE_TASK_COMPARISON "))
    )
    assert run.call_count == 2
    assert report["passed"] is False
    assert report["measurements"]["baseline"]["samples"] == [sample]
    assert report["measurements"]["baseline"]["exit_code"] == 124
    assert report["measurements"]["baseline"]["not_finished"] == 61
    assert len(report["measurements"]["current"]["samples"]) == 62
    assert "diagnostic" in lines[0] and "240-second cutoff" in lines[0]
