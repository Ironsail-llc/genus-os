"""A case the harness never let finish is not a case the agent failed.

Measured on the box 2026-08-24: `benchmark-runner` carries no explicit
`timeout_seconds`, so it inherits the fleet wall-clock ceiling of 3600s. A
full fleet sweep takes ~4.4h. At exactly 3600s the stall watchdog's hard
timeout cancels the task; the runner catches that cancellation, records a
TIMEOUT run, and returns instead of re-raising — so the kill lands on
whichever benchmark case happened to be in flight, the harness files it as
that agent's failure, and the sweep grinds on to take out another case an
hour later.

    Stall watchdog: hard timeout (3600s) reached after 3600s
    Agent crm-dedup cancelled: Run cancelled externally
    Benchmark task workflow-completes: timed out after 61s,
        well under its 900s budget — a smaller cap fired first

crm-dedup went 6/7 -> 4/7 that night having done nothing wrong. Thirteen
suite runs across nine agents carry this signature in the three days since
the diagnostic that names it was added; before that they were filed under the
configured budget and are unidentifiable.

The harness ALREADY detects this exactly — "well under its 900s budget" is
its own sentence. It then scores the case 0.0 and counts it. This module
pins the rule that closes that gap:

    elapsed ~= cap   -> the agent used its whole budget and did not finish.
                        That is the agent's failure. Counts.
    elapsed << cap   -> something else fired first. The agent was never given
                        its budget. Not measured, not counted, still loud.
"""

from __future__ import annotations

from robothor.engine.tools.handlers.benchmark import _score_suite, _timeout_result

_TASK = {"id": "workflow-completes", "category": "correctness", "weight": 1.0}


def _scored(task_id: str, score: float) -> dict:
    return {
        "task_id": task_id,
        "category": "correctness",
        "weight": 1.0,
        "score": score,
        "outcome": "scored",
    }


class TestTheResultKnowsWhichCapFired:
    def test_an_outer_kill_is_marked_not_measured(self) -> None:
        r = _timeout_result(_TASK, 900.0, "crm-dedup", "suite", elapsed_seconds=61.0)
        assert r["harness_kill"] is True

    def test_a_genuine_budget_overrun_is_the_agents_own(self) -> None:
        """The agent had its full budget and did not finish. That counts."""
        r = _timeout_result(_TASK, 900.0, "crm-dedup", "suite", elapsed_seconds=900.4)
        assert r["harness_kill"] is False

    def test_an_unmeasured_elapsed_is_not_assumed_to_be_a_kill(self) -> None:
        """Without a measurement there is no evidence of an outer cap, and
        inventing one would quietly drop real failures out of the denominator
        — the opposite mistake, and the more dangerous one."""
        r = _timeout_result(_TASK, 900.0, "crm-dedup", "suite")
        assert r["harness_kill"] is False


class TestTheGradeExcludesIt:
    def test_a_harness_kill_leaves_the_pass_rate_denominator(self) -> None:
        """6 passed, 1 killed by infrastructure => 6/6, not 6/7.

        This is the exact shape of crm-dedup's 2026-08-24 run.
        """
        results = [_scored(f"case-{i}", 1.0) for i in range(6)]
        results.append(_timeout_result(_TASK, 900.0, "crm-dedup", "suite", elapsed_seconds=61.0))

        record = _score_suite(results, suite_id="crm-dedup-v1", agent_id="crm-dedup")

        assert record["total_cases"] == 6
        assert record["passed"] == 6
        assert record["pass_rate"] == 1.0

    def test_a_real_timeout_still_counts_against_the_agent(self) -> None:
        results = [_scored(f"case-{i}", 1.0) for i in range(6)]
        results.append(_timeout_result(_TASK, 900.0, "crm-dedup", "suite", elapsed_seconds=901.0))

        record = _score_suite(results, suite_id="crm-dedup-v1", agent_id="crm-dedup")

        assert record["total_cases"] == 7
        assert record["passed"] == 6
        assert record["pass_rate"] < 1.0

    def test_the_kill_is_still_reported(self) -> None:
        """Excluded from the grade is not the same as swept under the rug.

        A sweep whose kills are climbing is broken infrastructure, and the
        number that says so must survive the exclusion — otherwise this fix
        would trade a wrong grade for a silent one.
        """
        results = [_scored("case-0", 1.0)]
        results.append(_timeout_result(_TASK, 900.0, "crm-dedup", "suite", elapsed_seconds=61.0))

        record = _score_suite(results, suite_id="crm-dedup-v1", agent_id="crm-dedup")

        assert record["timeouts"] == 1
        assert record["harness_kills"] == 1

    def test_a_suite_of_nothing_but_kills_is_not_a_perfect_score(self) -> None:
        """The dangerous edge: an empty denominator must not read as 100%.

        A sweep killed on its first case would otherwise report a flawless
        agent, which is exactly the failure this project keeps finding —
        an absence of evidence rendered as evidence of health.
        """
        results = [
            _timeout_result(_TASK, 900.0, "crm-dedup", "suite", elapsed_seconds=61.0),
            _timeout_result(_TASK, 900.0, "crm-dedup", "suite", elapsed_seconds=12.0),
        ]

        record = _score_suite(results, suite_id="crm-dedup-v1", agent_id="crm-dedup")

        assert record["pass_rate"] == 0.0
        assert record["harness_kills"] == 2
        assert record["measured"] is False, (
            "a suite that graded nothing must say so, not report a rate"
        )

    def test_a_normal_suite_is_marked_measured(self) -> None:
        record = _score_suite(
            [_scored("case-0", 1.0)], suite_id="crm-dedup-v1", agent_id="crm-dedup"
        )
        assert record["measured"] is True
