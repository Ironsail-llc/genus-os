"""Tests for the guardrail-event audit writer (Wave-1 hardening, PR-1).

``agent_guardrail_events`` has existed since migration 014 and is read by the
health dashboard, but nothing ever wrote to it. ``log_guardrail_event`` is the
best-effort writer that makes guardrail decisions (and, later, observe-mode
shadow decisions) visible before any enforcement is promoted.
"""

from __future__ import annotations

from robothor.engine.tracking import log_guardrail_event


class TestLogGuardrailEvent:
    def test_inserts_row_with_all_fields(self, mock_db):
        log_guardrail_event(
            run_id="run-1",
            guardrail_name="no_destructive_writes",
            action="blocked",
            tool_name="exec",
            reason="rm -rf blocked",
            mode="enforce",
            step_number=3,
        )
        mock_db["cursor"].execute.assert_called_once()
        sql = mock_db["cursor"].execute.call_args[0][0]
        assert "INSERT INTO agent_guardrail_events" in sql
        params = mock_db["cursor"].execute.call_args[0][1]
        assert params == (
            "run-1",
            3,
            "no_destructive_writes",
            "blocked",
            "exec",
            "rm -rf blocked",
            "enforce",
        )

    def test_observed_shadow_action(self, mock_db):
        """observe-mode shadow events use action='observed' so they're queryable."""
        log_guardrail_event(
            run_id="run-2",
            guardrail_name="rbac",
            action="observed",
            tool_name="delete_task",
            reason="would deny: role=service has no rule",
            mode="observe",
            step_number=5,
        )
        params = mock_db["cursor"].execute.call_args[0][1]
        assert params[1] == 5  # step_number
        assert params[3] == "observed"
        assert params[6] == "observe"  # mode

    def test_optional_fields_default_to_none(self, mock_db):
        log_guardrail_event(run_id="r", guardrail_name="g", action="allowed")
        params = mock_db["cursor"].execute.call_args[0][1]
        assert params == ("r", 0, "g", "allowed", None, None, None)

    def test_swallows_db_errors(self, mock_db):
        """Audit logging is best-effort and must never break the run."""
        mock_db["cursor"].execute.side_effect = RuntimeError("db down")
        # Must not raise.
        log_guardrail_event(run_id="r", guardrail_name="g", action="allowed")
