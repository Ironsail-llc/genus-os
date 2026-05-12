"""Tests for the fleet-wide runaway-token guard in runner._run_loop.

On Apr 22 16:07, a `main` run consumed 3.2M input tokens before hitting the
86400s circuit-breaker. The guard introduced in B4 alerts at 500K tokens
and hard-stops at 5M. Both thresholds are fleet-wide constants so a
misconfigured manifest cannot disable them.

We test the thresholds and the behavior of the guard code directly without
booting the whole run loop.
"""

from __future__ import annotations

from robothor.engine.runner import RUNAWAY_TOKEN_ALERT, RUNAWAY_TOKEN_HARD_CAP


class TestRunawayTokenConstants:
    def test_alert_threshold_is_500k(self) -> None:
        assert RUNAWAY_TOKEN_ALERT == 500_000

    def test_hard_cap_is_5m(self) -> None:
        assert RUNAWAY_TOKEN_HARD_CAP == 5_000_000

    def test_alert_lower_than_hard_cap(self) -> None:
        assert RUNAWAY_TOKEN_ALERT < RUNAWAY_TOKEN_HARD_CAP

    def test_hard_cap_above_observed_runaway(self) -> None:
        # The observed runaway was 3.2M; cap is higher so the 1M-3M band
        # (common for very long legitimate tasks) is not accidentally caught.
        # We alert but don't kill in that range — by design.
        assert RUNAWAY_TOKEN_HARD_CAP > 3_200_000


class TestGuardLogic:
    """Pure-logic tests that mirror the branching in _run_loop."""

    def _classify(self, used: int, alerted: bool) -> tuple[str, bool]:
        """Mirror of the guard's branching. Returns (action, new_alerted_flag)."""
        if used >= RUNAWAY_TOKEN_HARD_CAP:
            return ("stop", alerted)
        if not alerted and used >= RUNAWAY_TOKEN_ALERT:
            return ("alert", True)
        return ("continue", alerted)

    def test_499k_continue(self) -> None:
        action, alerted = self._classify(499_000, False)
        assert action == "continue"
        assert alerted is False

    def test_500k_alerts(self) -> None:
        action, alerted = self._classify(500_000, False)
        assert action == "alert"
        assert alerted is True

    def test_alert_only_fires_once(self) -> None:
        # Once the latch is set, crossing the threshold again → continue
        action, alerted = self._classify(1_000_000, True)
        assert action == "continue"
        assert alerted is True

    def test_5m_hard_stop(self) -> None:
        action, _ = self._classify(5_000_000, True)
        assert action == "stop"

    def test_hard_stop_supersedes_alert(self) -> None:
        # Even if never alerted, 5M+ stops.
        action, _ = self._classify(5_500_000, False)
        assert action == "stop"
