"""A run where nothing executed is not a score of zero.

The rotation hard-fails only when the harness produces no `summary.json`.
If it produces one, `ledger_entry` reads `mean_score` and appends the line —
without ever asking whether the tasks actually ran.

That gap has a live trigger. The instance's OpenRouter key is over its
weekly cap, and the bench agent pins `fallbacks: []` deliberately, so that
the comparison holds the model constant and varies only the harness. Every
LLM call therefore raises immediately, each task is graded against an empty
workspace, and the harness writes a perfectly well-formed summary whose
mean is 0.0. Tonight's rotation would append "0.0% vs baseline 38.8%,
delta -38.8%" to the ledger as though it were a measurement.

The ledger exists to accumulate runs and report a mean and a spread. One
fabricated zero poisons both, permanently, in the exact instrument built to
answer whether this platform leads its competitors.

`rotation.py`'s own docstring already states the rule — "a low score is a
result; only a failed RUN is a failure". A run in which no model ever
answered is a failed run. The discriminator is already recorded per task
and was simply never read: a task that consumed no tokens and issued no
requests did not execute.
"""

from __future__ import annotations

import pytest

from bench.wildclaw.rotation import EmptyRunError, ledger_entry

BASELINES = {"01_Productivity_Flow": {"mean": 0.3876, "tasks": {}}}


def _task(task_id: str, score: float, *, tokens: int, requests: int, status: str = "completed"):
    return {
        "task_id": task_id,
        "score": score,
        "usage": {
            "input_tokens": tokens,
            "output_tokens": max(0, tokens // 100),
            "request_count": requests,
            "status": status,
        },
    }


def _summary(results):
    return {
        "category": "01_Productivity_Flow",
        "tasks_attempted": len(results),
        "tasks_graded": len(results),
        "tasks_without_workspace": 0,
        "mean_score": round(sum(r["score"] for r in results) / len(results), 4) if results else 0.0,
        "results": results,
    }


class TestARealRunStillRecords:
    def test_the_2026_08_26_run_shape_is_accepted(self):
        """Mixed scores, some zeros, all executed — a genuine result."""
        s = _summary(
            [
                _task("t1", 0.12, tokens=2_822_216, requests=205),
                _task("t2", 0.00, tokens=1_865_039, requests=136, status="failed"),
                _task("t3", 0.96, tokens=1_161_229, requests=118),
            ]
        )
        entry = ledger_entry(s, BASELINES, when="2026-08-27T04:44:00")
        assert entry["mean"] == pytest.approx(0.36, abs=0.01)
        assert entry["tasks_executed"] == 3

    def test_a_genuine_zero_is_still_recorded(self):
        """Scoring zero having actually run is a result the ledger must keep."""
        s = _summary([_task("t1", 0.0, tokens=900_000, requests=70)])
        entry = ledger_entry(s, BASELINES, when="2026-08-27T04:44:00")
        assert entry["mean"] == 0.0
        assert entry["tasks_executed"] == 1


class TestNothingRan:
    def test_a_run_where_no_model_answered_is_refused(self):
        """The live case: every call 403s, so every task burns nothing."""
        s = _summary(
            [_task(f"t{i}", 0.0, tokens=0, requests=0, status="failed") for i in range(10)]
        )
        with pytest.raises(EmptyRunError):
            ledger_entry(s, BASELINES, when="2026-08-27T04:44:00")

    def test_the_refusal_says_what_to_check(self):
        s = _summary([_task("t1", 0.0, tokens=0, requests=0, status="failed")])
        with pytest.raises(EmptyRunError, match="no model answered|0 of 1"):
            ledger_entry(s, BASELINES, when="2026-08-27T04:44:00")

    def test_an_empty_result_set_is_refused(self):
        with pytest.raises(EmptyRunError):
            ledger_entry(_summary([]), BASELINES, when="2026-08-27T04:44:00")

    def test_one_survivor_is_enough_to_be_a_measurement(self):
        """Partial execution is a real, if thin, result — recorded, not refused.

        Refusing here would discard genuine data on a flaky night; the count
        rides along so a reader can weigh it.
        """
        results = [_task(f"t{i}", 0.0, tokens=0, requests=0, status="failed") for i in range(9)]
        results.append(_task("t9", 0.5, tokens=500_000, requests=40))
        entry = ledger_entry(_summary(results), BASELINES, when="2026-08-27T04:44:00")
        assert entry["tasks_executed"] == 1
        assert entry["tasks_attempted"] == 10


class TestEvidenceNotAbsence:
    """Only claim a task did not run when the harness said it consumed nothing."""

    def test_a_result_with_no_usage_block_is_assumed_to_have_run(self):
        """Summaries predating per-task usage must still be recordable."""
        s = _summary([{"task_id": "t1", "score": 0.4}])
        entry = ledger_entry(s, BASELINES, when="2026-08-27T04:44:00")
        assert entry["tasks_executed"] == 1

    def test_a_recorded_zero_usage_block_is_evidence_it_did_not(self):
        s = _summary([_task("t1", 0.0, tokens=0, requests=0, status="failed")])
        with pytest.raises(EmptyRunError):
            ledger_entry(s, BASELINES, when="2026-08-27T04:44:00")
