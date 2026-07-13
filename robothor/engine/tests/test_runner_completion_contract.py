"""Tests for the completion-contract call site in
``AgentRunner._after_response_delivered`` (PR-3a).

Flag-gated off→observe→enforce (default off, ``ROBOTHOR_COMPLETION_CONTRACTS_*``):
  - off:     no-op — check_completion_contract is never even called.
  - observe: computes + logs the verdict via ``log_guardrail_event`` but never
             mutates the CRM task.
  - enforce: on a ``missing`` verdict, writes a next_action nudge on the goal
             task (via ``dal.set_next_action``) so the task stays open.

These call ``_after_response_delivered`` directly (as
``test_runner_hooks.py`` does) rather than the full ``_finish_run`` path.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robothor.engine.completion_contract import ContractVerdict
from robothor.engine.config import EngineConfig
from robothor.engine.models import AgentRun, RunStatus
from robothor.engine.runner import AgentRunner
from robothor.engine.session import AgentSession


@pytest.fixture
def runner() -> AgentRunner:
    return AgentRunner(EngineConfig())


@pytest.fixture
def session() -> AgentSession:
    return AgentSession(agent_id="main")


def _run(output_text: str = "The task is complete.") -> AgentRun:
    return AgentRun(
        id="run-cc-1",
        tenant_id="default",
        agent_id="main",
        trigger_type="manual",  # type: ignore[arg-type]
        status=RunStatus.COMPLETED,
        output_text=output_text,
    )


class TestCompletionContractCallSite:
    @patch("robothor.engine.feature_flags.completion_contract_mode", return_value="off")
    @patch("robothor.engine.completion_contract.check_completion_contract")
    def test_mode_off_never_checks(self, mock_check, mock_mode, runner, session):
        runner._after_response_delivered(session, _run())
        mock_check.assert_not_called()

    @patch("robothor.crm.dal.set_next_action")
    @patch("robothor.engine.tracking.log_guardrail_event")
    @patch("robothor.engine.completion_contract.check_completion_contract")
    @patch("robothor.engine.feature_flags.completion_contract_mode", return_value="observe")
    def test_observe_logs_but_does_not_set_next_action(
        self, mock_mode, mock_check, mock_log, mock_set_next, runner, session
    ):
        mock_check.return_value = ContractVerdict(
            status="missing", goal_id="goal-1", missing=["no valid commit evidence"]
        )
        runner._after_response_delivered(session, _run())
        mock_check.assert_called_once()
        mock_log.assert_called_once()
        kwargs = mock_log.call_args.kwargs
        assert kwargs["guardrail_name"] == "completion_contract"
        assert kwargs["mode"] == "observe"
        assert kwargs["action"] == "observed"
        mock_set_next.assert_not_called()

    @patch("robothor.crm.dal.set_next_action")
    @patch("robothor.engine.tracking.log_guardrail_event")
    @patch("robothor.engine.completion_contract.check_completion_contract")
    @patch("robothor.engine.feature_flags.completion_contract_mode", return_value="enforce")
    def test_enforce_sets_next_action_when_missing(
        self, mock_mode, mock_check, mock_log, mock_set_next, runner, session
    ):
        mock_check.return_value = ContractVerdict(
            status="missing", goal_id="goal-1", missing=["no valid commit evidence"]
        )
        runner._after_response_delivered(session, _run())
        mock_set_next.assert_called_once()
        kwargs = mock_set_next.call_args.kwargs
        assert kwargs["task_id"] == "goal-1"
        assert "no valid commit evidence" in kwargs["next_action"]
        log_kwargs = mock_log.call_args.kwargs
        assert log_kwargs["action"] == "blocked"

    @patch("robothor.crm.dal.set_next_action")
    @patch("robothor.engine.tracking.log_guardrail_event")
    @patch("robothor.engine.completion_contract.check_completion_contract")
    @patch("robothor.engine.feature_flags.completion_contract_mode", return_value="enforce")
    def test_enforce_satisfied_does_not_set_next_action(
        self, mock_mode, mock_check, mock_log, mock_set_next, runner, session
    ):
        mock_check.return_value = ContractVerdict(status="satisfied", goal_id="goal-1", missing=[])
        runner._after_response_delivered(session, _run())
        mock_set_next.assert_not_called()

    @patch("robothor.crm.dal.set_next_action")
    @patch("robothor.engine.completion_contract.check_completion_contract")
    @patch("robothor.engine.feature_flags.completion_contract_mode", return_value="enforce")
    def test_no_verdict_is_a_noop(self, mock_mode, mock_check, mock_set_next, runner, session):
        mock_check.return_value = None
        runner._after_response_delivered(session, _run())
        mock_set_next.assert_not_called()

    @patch("robothor.engine.completion_contract.check_completion_contract")
    @patch("robothor.engine.feature_flags.completion_contract_mode", return_value="enforce")
    def test_check_exception_is_swallowed(self, mock_mode, mock_check, runner, session):
        mock_check.side_effect = RuntimeError("db down")
        # Must not raise — a broken contract check can never sink the run.
        runner._after_response_delivered(session, _run())
