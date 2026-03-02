"""Tests for dynamic replanning."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.escalation import THRESHOLD_DIFFERENT_STRATEGY, EscalationManager
from robothor.engine.planner import MAX_REPLANS, PlanResult, replan, should_replan
from robothor.engine.scratchpad import Scratchpad


class TestShouldReplan:
    def _plan(self) -> PlanResult:
        return PlanResult(
            success=True,
            plan=[
                {"step": 1, "action": "Read", "tool": "read_file"},
                {"step": 2, "action": "Process", "tool": "exec"},
                {"step": 3, "action": "Write", "tool": "write_file"},
            ],
            estimated_steps=3,
        )

    def _scratchpad(self, plan: PlanResult) -> Scratchpad:
        sp = Scratchpad()
        sp.set_plan(plan.plan)
        return sp

    def test_not_triggered_normal(self):
        plan = self._plan()
        sp = self._scratchpad(plan)
        esc = EscalationManager()
        assert should_replan(sp, plan, esc, 0) is False

    def test_triggered_consecutive_failures(self):
        plan = self._plan()
        sp = self._scratchpad(plan)
        esc = EscalationManager()

        # 3 failures on step 0
        sp.record_tool_call("read_file", error="fail")
        sp.record_tool_call("read_file", error="fail")
        sp.record_tool_call("read_file", error="fail")
        assert sp.current_step_attempts == 3
        assert should_replan(sp, plan, esc, 0) is True

    def test_triggered_budget_progress_mismatch(self):
        plan = self._plan()
        sp = self._scratchpad(plan)
        esc = EscalationManager()

        # 0% progress with 70% budget used
        assert should_replan(sp, plan, esc, 0, budget_pct_used=0.7) is True

    def test_not_triggered_good_progress(self):
        plan = self._plan()
        sp = self._scratchpad(plan)
        esc = EscalationManager()

        # Complete 2/3 steps — 66% progress
        sp.record_tool_call("read_file")
        sp.record_tool_call("exec")
        # Even with high budget, should NOT replan (progress >= 30%)
        assert should_replan(sp, plan, esc, 0, budget_pct_used=0.7) is False

    def test_triggered_escalation_threshold(self):
        plan = self._plan()
        sp = self._scratchpad(plan)
        esc = EscalationManager()

        for _ in range(THRESHOLD_DIFFERENT_STRATEGY):
            esc.record_error()
        assert esc.at_change_strategy_threshold
        assert should_replan(sp, plan, esc, 0) is True

    def test_blocked_at_max_replans(self):
        plan = self._plan()
        sp = self._scratchpad(plan)
        esc = EscalationManager()

        # Even with triggers, blocked at max replans
        sp.record_tool_call("read_file", error="fail")
        sp.record_tool_call("read_file", error="fail")
        sp.record_tool_call("read_file", error="fail")
        assert should_replan(sp, plan, esc, MAX_REPLANS) is False

    def test_blocked_with_failed_plan(self):
        plan = PlanResult(success=False)
        sp = Scratchpad()
        esc = EscalationManager()

        for _ in range(THRESHOLD_DIFFERENT_STRATEGY):
            esc.record_error()
        assert should_replan(sp, plan, esc, 0) is False

    def test_blocked_with_empty_plan(self):
        plan = PlanResult(success=True, plan=[])
        sp = Scratchpad()
        esc = EscalationManager()
        assert should_replan(sp, plan, esc, 0) is False


@pytest.mark.asyncio
async def test_replan_success():
    """Replanning produces a valid revised PlanResult."""
    original = PlanResult(
        success=True,
        plan=[
            {"step": 1, "action": "Read", "tool": "read_file"},
            {"step": 2, "action": "Process", "tool": "exec"},
        ],
    )
    sp = Scratchpad()
    sp.set_plan(original.plan)
    sp.record_tool_call("read_file")  # step 1 done
    sp.record_tool_call("exec", error="fail")  # step 2 failed

    revised_data = {
        "difficulty": "complex",
        "estimated_steps": 2,
        "plan": [
            {"step": 1, "action": "Try alternative", "tool": "web_fetch"},
            {"step": 2, "action": "Verify", "tool": "read_file"},
        ],
        "risks": ["Alternative may also fail"],
        "success_criteria": "Task complete",
    }

    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = json.dumps(revised_data)

    with patch("litellm.acompletion", return_value=response):
        result = await replan(original, sp, "test-model")

    assert result.success is True
    assert len(result.plan) == 2
    assert result.difficulty == "complex"
    assert result.plan[0]["tool"] == "web_fetch"


@pytest.mark.asyncio
async def test_replan_failure_returns_original():
    """Failed replanning returns the original plan."""
    original = PlanResult(
        success=True,
        plan=[{"step": 1, "action": "Do thing", "tool": "exec"}],
    )
    sp = Scratchpad()
    sp.set_plan(original.plan)

    with patch("litellm.acompletion", side_effect=Exception("API error")):
        result = await replan(original, sp, "bad-model")

    assert result is original


@pytest.mark.asyncio
async def test_replan_includes_progress_context():
    """Replan prompt includes information about completed and failed steps."""
    original = PlanResult(
        success=True,
        plan=[
            {"step": 1, "action": "Read inbox", "tool": "read_file"},
            {"step": 2, "action": "Classify", "tool": "exec"},
        ],
    )
    sp = Scratchpad()
    sp.set_plan(original.plan)
    sp.record_tool_call("read_file")  # completes step 1
    sp.record_tool_call("exec", error="failed")  # fails step 2

    captured_prompt = None

    async def capture_completion(**kwargs):
        nonlocal captured_prompt
        captured_prompt = kwargs["messages"][0]["content"]
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = json.dumps(
            {
                "difficulty": "moderate",
                "estimated_steps": 1,
                "plan": [{"step": 1, "action": "retry", "tool": "exec"}],
                "risks": [],
                "success_criteria": "",
            }
        )
        return response

    with patch("litellm.acompletion", side_effect=capture_completion):
        await replan(original, sp, "test-model")

    assert captured_prompt is not None
    assert "completed" in captured_prompt.lower()
    assert "failed" in captured_prompt.lower()
