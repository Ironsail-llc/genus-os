"""The hard abort must count a run that is failing, not a run that has failed before.

`HARD_ABORT_TOTAL_ERRORS = 10` counted CUMULATIVE tool errors and never reset,
so an agent that hit ten errors spread across a whole run was force-stopped even
if the last nine calls had all succeeded. The module docstring already said
"Resets on success"; only `consecutive_errors` did.

MEASURED 2026-09-15, one benchmark task:

    "error": "Too many errors (11 total). Summarize progress."

That run ended at 341 seconds of a 1,200-second budget with a score of 0.0.
Against the web these tasks actually use — 403s from one conference site, 429s
from two APIs, a bot check on a third — ten cumulative failures is a low bar,
and the whole point of retrying against a hostile network is that some of the
retries work.

So the budget is now a WINDOW: recovery pays some of it back. Not a full reset —
an agent that fails ten times, succeeds once and fails ten more is still a
broken run, and the abort has to survive that.
"""

from __future__ import annotations

from robothor.engine.escalation import (
    ERRORS_FORGIVEN_PER_SUCCESS,
    HARD_ABORT_TOTAL_ERRORS,
    EscalationManager,
)


class TestARunThatKeepsFailingStillAborts:
    def test_unbroken_failures_abort_at_the_threshold(self):
        manager = EscalationManager()
        for _ in range(HARD_ABORT_TOTAL_ERRORS - 1):
            manager.record_error()
        assert not manager.should_abort()
        manager.record_error()
        assert manager.should_abort()

    def test_one_success_buys_exactly_one_more_error(self):
        """Not a full reset. A reset would make the abort unreachable for any
        agent that ever succeeds, which is every agent."""
        manager = EscalationManager()
        for _ in range(HARD_ABORT_TOTAL_ERRORS):
            manager.record_error()
        assert manager.should_abort()
        manager.record_success()
        assert not manager.should_abort(), "recovery has to be worth something"
        manager.record_error()
        assert manager.should_abort(), "and worth exactly one error, not ten"

    def test_a_run_that_alternates_badly_still_aborts(self):
        """Nine failures, one success, nine more failures is a broken run
        whichever way the counter is read."""
        manager = EscalationManager()
        for _ in range(9):
            manager.record_error()
        manager.record_success()
        for _ in range(9):
            manager.record_error()
        assert manager.should_abort()


class TestRecoveryPaysSomeOfItBack:
    def test_a_success_forgives_a_bounded_number_of_errors(self):
        assert ERRORS_FORGIVEN_PER_SUCCESS >= 1

    def test_an_agent_that_recovers_from_every_failure_keeps_working(self):
        """The measured shape: errors spread across a long run against a
        hostile network, each one recovered from. Forty failures and forty
        recoveries is a working run, and the old counter killed it at ten."""
        manager = EscalationManager()
        for _ in range(40):
            manager.record_error()
            manager.record_success()
        assert not manager.should_abort(), "a recovering run was force-stopped"
        assert manager.total_errors == 40

    def test_a_run_failing_faster_than_it_recovers_still_dies(self):
        """Two failures for every success is not a run recovering from a
        hostile network, it is a run that is not working."""
        manager = EscalationManager()
        for _ in range(HARD_ABORT_TOTAL_ERRORS + 1):
            manager.record_error()
            manager.record_error()
            manager.record_success()
        assert manager.should_abort()

    def test_forgiveness_never_goes_negative(self):
        """A run that succeeds a hundred times must not bank a hundred free
        failures — the threshold would then mean nothing for the rest of it."""
        manager = EscalationManager()
        for _ in range(100):
            manager.record_success()
        for _ in range(HARD_ABORT_TOTAL_ERRORS):
            manager.record_error()
        assert manager.should_abort()


class TestTheCountsStayHonest:
    def test_total_errors_is_still_the_true_total(self):
        """It is reported to operators and read by the run summary. Forgiveness
        belongs to the abort decision, not to the record of what happened."""
        manager = EscalationManager()
        for _ in range(5):
            manager.record_error()
            manager.record_success()
        assert manager.total_errors == 5

    def test_consecutive_errors_still_resets(self):
        manager = EscalationManager()
        manager.record_error()
        manager.record_error()
        manager.record_success()
        assert manager.consecutive_errors == 0
