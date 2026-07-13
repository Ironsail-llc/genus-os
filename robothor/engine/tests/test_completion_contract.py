"""Tests for evidence-based completion contracts (PR-3a).

Hermes-style headline feature grafted onto the existing session-goal
evidence engine: when an agent's final output *claims* a goal is done, the
claim is checked against ``missing_completion_requirements`` instead of
trusting the model's say-so. Pure module — no DB, no LLM; DAL calls are
mocked via ``session_goal.get_active_goal``.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

from robothor.engine.completion_contract import ContractVerdict, check_completion_contract
from robothor.engine.session_goal import GoalEvidence, SessionGoal


@dataclass
class _FakeRun:
    tenant_id: str = "default"
    agent_id: str = "main"
    output_text: str | None = None


@dataclass
class _FakeConfig:
    workspace: str = "/tmp/does-not-matter"


def _goal(*, evidence: list[GoalEvidence] | None = None, criteria: list[str] | None = None):
    return SessionGoal(
        id="goal-1",
        objective="ship the feature",
        success_criteria=criteria or ["it works"],
        agent_id="main",
        status="active",
        evidence=evidence or [],
        completion_note="",
    )


class TestCheckCompletionContract:
    @patch("robothor.engine.completion_contract.get_active_goal")
    def test_no_active_goal_returns_none(self, mock_get_goal):
        mock_get_goal.return_value = None
        run = _FakeRun(output_text="Done! The task is complete.")
        assert check_completion_contract(run, _FakeConfig()) is None

    @patch("robothor.engine.completion_contract.get_active_goal")
    def test_active_goal_but_no_completion_claim_returns_none(self, mock_get_goal):
        mock_get_goal.return_value = _goal()
        run = _FakeRun(output_text="Still working through the remaining steps.")
        assert check_completion_contract(run, _FakeConfig()) is None

    @patch("robothor.engine.completion_contract.get_active_goal")
    def test_claims_completion_with_missing_evidence_returns_missing_verdict(self, mock_get_goal):
        mock_get_goal.return_value = _goal(evidence=[])
        run = _FakeRun(output_text="The task is complete — all done.")
        verdict = check_completion_contract(run, _FakeConfig())
        assert isinstance(verdict, ContractVerdict)
        assert verdict.status == "missing"
        assert verdict.goal_id == "goal-1"
        assert verdict.missing

    @patch(
        "robothor.engine.completion_contract.missing_completion_requirements",
        return_value=[],
    )
    @patch("robothor.engine.completion_contract.get_active_goal")
    def test_claims_completion_with_satisfied_evidence_returns_satisfied_verdict(
        self, mock_get_goal, _mock_missing
    ):
        mock_get_goal.return_value = _goal(
            evidence=[
                GoalEvidence(kind="test_run", summary="green", reference="pytest:passed:10"),
                GoalEvidence(kind="commit", summary="shipped", reference="abcdef1234567"),
            ]
        )
        run = _FakeRun(output_text="I have completed the task.")
        verdict = check_completion_contract(run, _FakeConfig())
        assert verdict is not None
        assert verdict.status == "satisfied"
        assert verdict.missing == []

    @patch("robothor.engine.completion_contract.get_active_goal")
    def test_negated_completion_language_is_not_a_claim(self, mock_get_goal):
        mock_get_goal.return_value = _goal()
        run = _FakeRun(output_text="The task is not complete yet; more work remains.")
        assert check_completion_contract(run, _FakeConfig()) is None

    @patch("robothor.engine.completion_contract.get_active_goal")
    def test_empty_output_text_returns_none(self, mock_get_goal):
        mock_get_goal.return_value = _goal()
        run = _FakeRun(output_text=None)
        assert check_completion_contract(run, _FakeConfig()) is None


class TestClaimsCompletionHeuristic:
    """Direct tests of the private claim-detection heuristic."""

    def test_detects_common_completion_phrasings(self):
        from robothor.engine.completion_contract import _claims_completion

        assert _claims_completion("The task is complete.")
        assert _claims_completion("I have completed the goal.")
        assert _claims_completion("Marking this goal as complete.")
        assert _claims_completion("This completes the objective.")
        assert _claims_completion("Goal complete — see summary above.")

    def test_does_not_flag_unrelated_or_negated_text(self):
        from robothor.engine.completion_contract import _claims_completion

        assert not _claims_completion(None)
        assert not _claims_completion("")
        assert not _claims_completion("Still investigating the failure.")
        assert not _claims_completion("The task is not complete.")
        assert not _claims_completion("I have not completed the goal yet.")
