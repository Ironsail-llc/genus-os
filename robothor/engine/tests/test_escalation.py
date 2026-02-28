"""Tests for graduated escalation."""

from __future__ import annotations

from robothor.engine.escalation import (
    HARD_ABORT_TOTAL_ERRORS,
    THRESHOLD_DIFFERENT_STRATEGY,
    THRESHOLD_REDUCE_SCOPE,
    THRESHOLD_STOP,
    EscalationManager,
)


class TestEscalationManager:
    def test_no_escalation_below_threshold(self):
        mgr = EscalationManager()
        mgr.record_error()
        mgr.record_error()
        assert mgr.get_escalation_message() is None

    def test_different_strategy_at_threshold(self):
        mgr = EscalationManager()
        for _ in range(THRESHOLD_DIFFERENT_STRATEGY):
            mgr.record_error()
        msg = mgr.get_escalation_message()
        assert msg is not None
        assert "DIFFERENT" in msg.upper()

    def test_reduce_scope_at_threshold(self):
        mgr = EscalationManager()
        for _ in range(THRESHOLD_REDUCE_SCOPE):
            mgr.record_error()
        msg = mgr.get_escalation_message()
        assert msg is not None
        assert "REDUCE SCOPE" in msg.upper()

    def test_stop_at_threshold(self):
        mgr = EscalationManager()
        for _ in range(THRESHOLD_STOP):
            mgr.record_error()
        msg = mgr.get_escalation_message()
        assert msg is not None
        assert "STOP" in msg.upper()

    def test_stop_only_issued_once(self):
        mgr = EscalationManager()
        for _ in range(THRESHOLD_STOP):
            mgr.record_error()
        msg1 = mgr.get_escalation_message()
        assert "STOP" in msg1.upper()

        mgr.record_error()
        msg2 = mgr.get_escalation_message()
        # After stop is issued once, no more stop messages
        assert msg2 is None or "STOP" not in msg2.upper()

    def test_success_resets_consecutive(self):
        mgr = EscalationManager()
        mgr.record_error()
        mgr.record_error()
        mgr.record_success()
        assert mgr.consecutive_errors == 0
        assert mgr.total_errors == 2  # total doesn't reset

    def test_should_abort_at_hard_ceiling(self):
        mgr = EscalationManager()
        for _ in range(HARD_ABORT_TOTAL_ERRORS):
            mgr.record_error()
        assert mgr.should_abort() is True

    def test_should_not_abort_below_ceiling(self):
        mgr = EscalationManager()
        for _ in range(HARD_ABORT_TOTAL_ERRORS - 1):
            mgr.record_error()
        assert mgr.should_abort() is False
