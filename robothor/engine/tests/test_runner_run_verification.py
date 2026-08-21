"""Tests for the run-verification call site in ``AgentRunner._finish_run``.

The verdict has to be computed BEFORE ``_persist_run``, because
``_assess_outcome`` runs inside ``_persist_run_sync`` and the persisted
``agent_runs`` row carries ``verified_status`` / ``verification``.

Flag ladder ``ROBOTHOR_RUN_VERIFICATION_ENABLED`` / ``_MODE``:
  - off:     no-op — ``verify_run`` is never called, the run is untouched.
  - observe: computes the verdict, stamps it on the run, logs a guardrail
             event. Nothing else changes (no delivery/task side effects).
  - alert:   observe + an operator notification.
  - enforce: same recording as alert for now — acting on the verdict
             (delivery gating, task resolution) belongs to the follow-up PR.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robothor.engine.config import EngineConfig
from robothor.engine.models import AgentRun, RunStatus, RunStep, StepType
from robothor.engine.run_verification import Verdict
from robothor.engine.runner import AgentRunner


@pytest.fixture
def runner() -> AgentRunner:
    return AgentRunner(EngineConfig())


def _run(output_text: str = "✅ Payment confirmed — $270 sent to Casey via Venmo.") -> AgentRun:
    run = AgentRun(
        id="00000000-0000-0000-0000-0000000000rv",
        tenant_id="default",
        agent_id="main",
        trigger_type="telegram",  # type: ignore[arg-type]
        status=RunStatus.COMPLETED,
        output_text=output_text,
    )
    run.steps = [
        RunStep(
            run_id=run.id,
            step_number=2,
            step_type=StepType.TOOL_CALL,
            tool_name="write_file",
            tool_input={"path": "/tmp/payment_note.md", "content": "…"},
            tool_output={"success": True},
        )
    ]
    return run


class TestRunVerificationCallSite:
    @patch("robothor.engine.feature_flags.run_verification_mode", return_value="off")
    @patch("robothor.engine.run_verification.verify_run")
    def test_mode_off_never_verifies(self, mock_verify, _mode, runner):
        run = _run()
        with patch.object(AgentRunner, "_persist_run_sync"):
            runner._finish_run(run)
        mock_verify.assert_not_called()
        assert run.verified_status is None
        assert run.verification is None

    @patch("robothor.engine.tracking.log_guardrail_event")
    @patch("robothor.engine.feature_flags.run_verification_mode", return_value="observe")
    def test_observe_stamps_the_verdict_on_the_run(self, _mode, mock_log, runner):
        run = _run()
        with patch.object(AgentRunner, "_persist_run_sync"):
            runner._finish_run(run)
        assert run.verified_status == "unverified_claims"
        assert run.verification is not None
        assert any(c["kind"] == "payment" for c in run.verification["claims"])
        mock_log.assert_called_once()
        kwargs = mock_log.call_args.kwargs
        assert kwargs["guardrail_name"] == "run_verification"
        assert kwargs["mode"] == "observe"
        assert kwargs["action"] == "observed"

    @patch("robothor.engine.tracking.log_guardrail_event")
    @patch("robothor.engine.feature_flags.run_verification_mode", return_value="observe")
    def test_verdict_is_computed_before_persistence(self, _mode, _log, runner):
        """``_persist_run_sync`` must see a run that already carries the verdict."""
        seen: list[str | None] = []

        def _capture(self, run):  # noqa: ANN001
            seen.append(run.verified_status)

        with patch.object(AgentRunner, "_persist_run_sync", _capture):
            runner._finish_run(_run())
        assert seen == ["unverified_claims"]

    @patch("robothor.engine.tracking.log_guardrail_event")
    @patch("robothor.engine.feature_flags.run_verification_mode", return_value="observe")
    def test_no_claims_records_nothing(self, _mode, mock_log, runner):
        run = _run(output_text="Here are three restaurants you might like.")
        with patch.object(AgentRunner, "_persist_run_sync"):
            runner._finish_run(run)
        assert run.verified_status == "no_claims"
        mock_log.assert_not_called()

    @patch("robothor.engine.feature_flags.notify_guardrail_alert")
    @patch("robothor.engine.tracking.log_guardrail_event")
    @patch("robothor.engine.feature_flags.run_verification_mode", return_value="alert")
    def test_alert_notifies_the_operator(self, _mode, _log, mock_notify, runner):
        with patch.object(AgentRunner, "_persist_run_sync"):
            runner._finish_run(_run())
        mock_notify.assert_called_once()
        assert mock_notify.call_args.kwargs["guardrail_name"] == "run_verification"

    @patch("robothor.engine.feature_flags.notify_guardrail_alert")
    @patch("robothor.engine.tracking.log_guardrail_event")
    @patch("robothor.engine.feature_flags.run_verification_mode", return_value="observe")
    def test_observe_does_not_notify(self, _mode, _log, mock_notify, runner):
        with patch.object(AgentRunner, "_persist_run_sync"):
            runner._finish_run(_run())
        mock_notify.assert_not_called()

    @patch("robothor.engine.feature_flags.run_verification_mode", return_value="observe")
    @patch("robothor.engine.run_verification.verify_run", side_effect=RuntimeError("boom"))
    def test_verification_failure_never_breaks_the_run(self, _verify, _mode, runner):
        run = _run()
        with patch.object(AgentRunner, "_persist_run_sync"):
            assert runner._finish_run(run) is run
        assert run.verified_status is None

    @patch("robothor.engine.tracking.log_guardrail_event")
    @patch("robothor.engine.feature_flags.run_verification_mode", return_value="observe")
    def test_observe_does_not_touch_delivery_or_tasks(self, _mode, _log, runner):
        """Observe is bookkeeping: delivery and task fields must be untouched."""
        run = _run()
        with (
            patch("robothor.crm.dal.set_next_action") as mock_next,
            patch("robothor.crm.dal.update_task") as mock_update,
            patch.object(AgentRunner, "_persist_run_sync"),
        ):
            runner._finish_run(run)
        mock_next.assert_not_called()
        mock_update.assert_not_called()
        assert run.delivery_status is None


class TestAssessOutcomeSeesTheVerdict:
    def test_unverified_claims_are_noted_in_outcome_notes(self):
        run = _run()
        run.verified_status = "unverified_claims"
        run.verification = {
            "status": "unverified_claims",
            "unsupported": ["payment"],
            "claims": [],
        }
        AgentRunner._assess_outcome(run)
        assert run.outcome_notes is not None
        assert "unverified" in run.outcome_notes.lower()
        assert "payment" in run.outcome_notes

    def test_outcome_assessment_itself_is_unchanged(self):
        """This PR records; it does not re-grade. A later PR owns acting on it."""
        run = _run(output_text="x" * 200)
        run.verified_status = "unverified_claims"
        run.verification = {"status": "unverified_claims", "unsupported": ["payment"]}
        AgentRunner._assess_outcome(run)
        assert run.outcome_assessment == "successful"

    def test_no_verdict_leaves_notes_alone(self):
        run = _run(output_text="x" * 200)
        AgentRunner._assess_outcome(run)
        assert run.outcome_assessment == "successful"
        assert run.outcome_notes is None


class TestPersistedColumns:
    def test_update_run_accepts_the_verification_columns(self):
        """``tracking.update_run`` must be able to write the migration-100 columns."""
        import robothor.engine.tracking as tracking

        captured: dict[str, object] = {}

        class _Cur:
            def execute(self, sql, values):
                captured["sql"] = sql
                captured["values"] = values

            rowcount = 1

        class _Conn:
            def cursor(self):
                return _Cur()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch.object(tracking, "get_connection", lambda: _Conn()):
            tracking.update_run(
                "run-1",
                verified_status="unverified_claims",
                verification={"status": "unverified_claims"},
            )
        assert "verified_status = %s" in captured["sql"]
        assert "verification = %s" in captured["sql"]


class TestVerdictContract:
    def test_verdict_statuses_are_the_documented_four(self):
        from robothor.engine.run_verification import VERIFICATION_STATUSES

        assert set(VERIFICATION_STATUSES) == {
            "no_claims",
            "verified",
            "unverified_claims",
            "failed_verification",
        }

    def test_verdict_payload_round_trips_through_json(self):
        import json

        verdict = Verdict(status="no_claims")
        assert json.loads(json.dumps(verdict.to_payload()))["status"] == "no_claims"
