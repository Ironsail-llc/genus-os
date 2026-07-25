"""Map an eval report onto a benchmark_results row.

The memory eval already returns a structured report and already exits non-zero
on failure, but nothing consumes either — no schedule runs it and no row lands
in benchmark_results, so the fleet's daily grader cannot see it. "12/12" has
therefore only ever been a point measurement someone ran by hand.

The mapping is pure so it can be tested without a database, and so the shape
that reaches the table is pinned rather than discovered later from a dashboard
that looks wrong.

The suite is deliberately NOT rewritten into the fleet's `tasks:` form. That
form runs an agent and pattern-matches its prose; this suite seeds a known fact
and checks whether retrieval returns it. Converting would replace deterministic
ground truth with an LLM's wording.
"""

from __future__ import annotations

import pytest

from robothor.memory.eval import report_to_benchmark_row


def _report(**over):
    base = {
        "suite_id": "memory-recall-v1",
        "total": 12,
        "passed": 12,
        "by_kind": {
            "recall": {"passed": 2, "total": 2},
            "temporal": {"passed": 4, "total": 4},
            "verbatim": {"passed": 2, "total": 2},
        },
        "cases": [],
    }
    base.update(over)
    return base


class TestRowShape:
    def test_identifies_itself_as_the_memory_suite(self):
        row = report_to_benchmark_row(_report(), suite_path="docs/benchmarks/memory/suite.yaml")
        assert row["agent_id"] == "memory"
        assert row["suite_id"] == "memory-recall-v1"
        assert row["suite_path"] == "docs/benchmarks/memory/suite.yaml"

    def test_pass_rate_is_strict_per_case(self):
        row = report_to_benchmark_row(_report(passed=9, total=12))
        assert row["pass_rate"] == pytest.approx(0.75)
        assert row["passed"] == 9 and row["failed"] == 3

    def test_category_scores_carry_per_stratum_rates(self):
        """Stratum regressions must surface in the dashboards that already read
        this column, rather than needing a new surface built for them."""
        row = report_to_benchmark_row(_report(by_kind={"temporal": {"passed": 2, "total": 4}}))
        assert row["category_scores"]["temporal"] == pytest.approx(0.5)

    def test_cost_is_zero_not_absent(self):
        """Local Ollama has no per-call price. Recording 0.0 keeps the column
        meaningful; omitting it would read as missing data."""
        assert report_to_benchmark_row(_report())["cost_usd"] == 0.0

    def test_failures_are_summarised_for_triage(self):
        rep = _report(
            passed=1,
            total=2,
            cases=[
                {"case_id": "temporal-x", "kind": "temporal", "passed": False, "score": 0.0,
                 "detail": "gold not in top-5; a wrong fact outranked it"},
                {"case_id": "recall-y", "kind": "recall", "passed": True, "score": 1.0,
                 "detail": ""},
            ],
        )
        row = report_to_benchmark_row(rep)
        assert len(row["failures"]) == 1
        f = row["failures"][0]
        assert f["case_id"] == "temporal-x" and f["category"] == "temporal"
        assert "outranked" in f["output_preview"]

    def test_triggered_by_defaults_to_manual_and_is_overridable(self):
        assert report_to_benchmark_row(_report())["triggered_by"] == "manual"
        assert report_to_benchmark_row(_report(), triggered_by="cron")["triggered_by"] == "cron"


class TestVacuousGuards:
    def test_zero_cases_is_not_a_perfect_score(self):
        """0/0 must not serialise as pass_rate 1.0. A gate that grades an empty
        suite green is how it ends up certifying nothing."""
        row = report_to_benchmark_row(_report(total=0, passed=0, by_kind={}))
        assert row["pass_rate"] == 0.0
        assert row["total_cases"] == 0

    def test_missing_by_kind_does_not_crash(self):
        row = report_to_benchmark_row({"suite_id": "s", "total": 1, "passed": 1})
        assert row["category_scores"] == {}
