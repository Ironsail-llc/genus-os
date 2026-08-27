"""A run the engine never let finish is not a run the agent failed.

Restarting the engine cancels every in-flight run. The runner records those
as `status='timeout'` with `error_message` starting "Run cancelled
externally", and `analytics` counts any timeout row toward `timeouts`,
which `compute_goal_metrics` divides into `timeout_rate`. Main's manifest
grades `timeout_rate < 0.05` at weight 2.0.

So deploying lowers the grade of every agent that happened to be running.
Measured on this instance 2026-08-27: **89 of 147 timeouts in seven days
(61%) were external cancellations**, 17 of them main's, most from that
day's own deploys.

The benchmark harness already learned this — see
`test_harness_kill_is_not_a_grade`, "a case the harness never let finish is
not a case the agent failed". That fix stopped at the benchmark scorer; the
goal metric one layer up kept counting them.

The counters are spread across several queries, so the predicate is shared
and a drift test pins that they all use it. This repo has been bitten three
times by a second copy of a rule that stopped matching the first.
"""

from __future__ import annotations

import re
from pathlib import Path

from robothor.engine.analytics import (
    EXTERNAL_CANCEL_PREFIX,
    GENUINE_TIMEOUT_SQL,
    INTERRUPTED_SQL,
    is_external_cancellation,
)

ENGINE = Path(__file__).resolve().parents[1]


class TestThePredicate:
    def test_a_restart_cancellation_is_not_a_timeout(self):
        assert is_external_cancellation("Run cancelled externally; last activity: session_started")

    def test_a_real_stall_is_a_timeout(self):
        assert not is_external_cancellation(
            "No progress for 144s (stall limit 120s); last activity: tool:get_knowledge_gaps"
        )

    def test_a_circuit_breaker_hard_timeout_is_a_timeout(self):
        """The agent really did exceed its configured ceiling."""
        assert not is_external_cancellation(
            "Circuit-breaker hard timeout (3600s); last activity: tool:x"
        )

    def test_missing_message_is_not_assumed_external(self):
        assert not is_external_cancellation(None)
        assert not is_external_cancellation("")

    def test_sql_and_python_agree_on_the_marker(self):
        assert EXTERNAL_CANCEL_PREFIX in GENUINE_TIMEOUT_SQL
        assert EXTERNAL_CANCEL_PREFIX in INTERRUPTED_SQL
        assert "NOT LIKE" in GENUINE_TIMEOUT_SQL
        assert "NOT LIKE" not in INTERRUPTED_SQL


class TestNoCounterIsLeftBehind:
    """Every quality-signal timeout counter must use the shared predicate."""

    def _sources(self) -> list[Path]:
        files = [
            ENGINE / "analytics.py",
            ENGINE / "tracking.py",
        ]
        for f in files:
            assert f.exists(), f"{f} missing — this scan would pass over nothing"
        return files

    def test_the_scan_actually_reads_files(self):
        """Guard against the scan silently covering an empty set."""
        bodies = [f.read_text() for f in self._sources()]
        assert all(len(b) > 1000 for b in bodies)
        assert any("timeouts" in b for b in bodies)

    def test_no_raw_timeout_counter_remains(self):
        raw = re.compile(r"COUNT\(\*\)\s*FILTER\s*\(\s*WHERE\s+status\s*=\s*'timeout'\s*\)")
        offenders = []
        for f in self._sources():
            for n, line in enumerate(f.read_text().splitlines(), 1):
                if raw.search(line):
                    offenders.append(f"{f.name}:{n}")
        assert not offenders, "these count a cancelled run as the agent's timeout: " + ", ".join(
            offenders
        )
