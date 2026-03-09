"""
Integration test — validates the full error recovery chain with real config loading.

Marked @pytest.mark.integration — skipped in pre-commit, run explicitly.
Uses mocked LLM but real config parsing and model structures.
"""

from __future__ import annotations

import pytest

from robothor.engine.error_recovery import classify_error, get_recovery_action
from robothor.engine.escalation import EscalationManager
from robothor.engine.models import AgentConfig, DeliveryMode, ErrorType
from robothor.engine.planner import PlanResult, should_replan
from robothor.engine.scratchpad import Scratchpad


@pytest.mark.integration
class TestAgenticIntegration:
    """End-to-end integration of error classification → recovery → replanning."""

    def test_full_error_recovery_chain(self):
        """Simulate a multi-step run: tool fails → classify → recover → replan."""
        # 1. Create realistic config
        config = AgentConfig(
            id="integration-test",
            name="Integration Test Agent",
            model_primary="openrouter/z-ai/glm-5",
            model_fallbacks=["openrouter/minimax/minimax-m2.5"],
            delivery_mode=DeliveryMode.NONE,
            can_spawn_agents=True,
            max_nesting_depth=2,
            planning_enabled=True,
            scratchpad_enabled=True,
            error_feedback=True,
        )

        # 2. Simulate plan
        plan = PlanResult(
            success=True,
            difficulty="moderate",
            estimated_steps=4,
            plan=[
                {"step": 1, "action": "Read config", "tool": "read_file"},
                {"step": 2, "action": "Fetch API data", "tool": "web_fetch"},
                {"step": 3, "action": "Process data", "tool": "exec"},
                {"step": 4, "action": "Write results", "tool": "write_file"},
            ],
        )

        # 3. Initialize tracking objects
        scratchpad = Scratchpad()
        scratchpad.set_plan(plan.plan)
        escalation = EscalationManager()

        # 4. Step 1 succeeds
        scratchpad.record_tool_call("read_file")
        escalation.record_success()
        assert scratchpad.steps_completed == 1
        assert scratchpad._current_step == 1

        # 5. Step 2 fails with auth error
        error_msg = "403 Forbidden: API key expired"
        error_type = classify_error("web_fetch", error_msg)
        assert error_type == ErrorType.AUTH

        scratchpad.record_tool_call("web_fetch", error=error_msg)
        escalation.record_error(error_type)

        # First auth failure — no recovery action yet
        action = get_recovery_action(
            error_type, 1, config, "web_fetch", error_msg, helper_spawns_used=0
        )
        assert action is None

        # 6. Step 2 fails again
        scratchpad.record_tool_call("web_fetch", error=error_msg)
        escalation.record_error(error_type)

        # Second auth failure — should trigger helper spawn
        action = get_recovery_action(
            error_type, 2, config, "web_fetch", error_msg, helper_spawns_used=0
        )
        assert action is not None
        assert action.action == "spawn"
        assert "web_fetch" in action.message

        # 7. Step 2 fails a third time (even after helper)
        scratchpad.record_tool_call("web_fetch", error=error_msg)
        escalation.record_error(error_type)

        # Check replanning triggers (3 failures on same step)
        assert scratchpad.current_step_attempts == 3
        assert should_replan(scratchpad, plan, escalation, replan_count=0) is True

        # 8. Simulate replan — new plan skips the broken API
        new_plan = PlanResult(
            success=True,
            plan=[
                {"step": 1, "action": "Use cached data", "tool": "read_file"},
                {"step": 2, "action": "Process cached data", "tool": "exec"},
            ],
        )
        scratchpad.set_plan(new_plan.plan)

        # 9. Continue with new plan
        scratchpad.record_tool_call("read_file")
        escalation.record_success()
        scratchpad.record_tool_call("exec")
        escalation.record_success()

        assert scratchpad.steps_completed == 2
        assert scratchpad._current_step == 2

        # 10. Verify final state
        summary = scratchpad.format_summary()
        assert "2/2 complete" in summary

    def test_error_type_coverage(self):
        """Verify all error types are properly classified."""
        test_cases = [
            ("401 Unauthorized", ErrorType.AUTH),
            ("403 Forbidden", ErrorType.AUTH),
            ("429 Too Many Requests", ErrorType.RATE_LIMIT),
            ("404 Not Found", ErrorType.NOT_FOUND),
            ("TimeoutError", ErrorType.TIMEOUT),
            ("ModuleNotFoundError: No module named 'x'", ErrorType.DEPENDENCY),
            ("PermissionError: [Errno 13]", ErrorType.PERMISSION),
            ("API deprecated since v2", ErrorType.API_DEPRECATED),
            ("random error", ErrorType.UNKNOWN),
        ]
        for msg, expected_type in test_cases:
            result = classify_error("test_tool", msg)
            assert result == expected_type, f"Expected {expected_type} for '{msg}', got {result}"

    def test_recovery_action_matrix(self):
        """Verify recovery actions for all error type + count combinations."""
        config_spawn = AgentConfig(id="test", name="test", can_spawn_agents=True)
        config_no_spawn = AgentConfig(id="test", name="test", can_spawn_agents=False)

        # Rate limit: always backoff, never spawn
        for count in range(1, 5):
            action = get_recovery_action(ErrorType.RATE_LIMIT, count, config_spawn, "tool", "429")
            assert action is not None
            assert action.action == "backoff"

        # Auth: None at 1, spawn at 2+ (if can_spawn)
        assert get_recovery_action(ErrorType.AUTH, 1, config_spawn, "t", "401") is None
        auth_action = get_recovery_action(ErrorType.AUTH, 2, config_spawn, "t", "401")
        assert auth_action is not None
        assert auth_action.action == "spawn"
        assert get_recovery_action(ErrorType.AUTH, 2, config_no_spawn, "t", "401") is None

        # Timeout: retry at 1, spawn at 2+
        timeout_retry = get_recovery_action(ErrorType.TIMEOUT, 1, config_spawn, "t", "timeout")
        assert timeout_retry is not None
        assert timeout_retry.action == "retry"
        timeout_spawn = get_recovery_action(ErrorType.TIMEOUT, 2, config_spawn, "t", "timeout")
        assert timeout_spawn is not None
        assert timeout_spawn.action == "spawn"

        # Permission: always inject
        perm_action = get_recovery_action(ErrorType.PERMISSION, 1, config_spawn, "t", "denied")
        assert perm_action is not None
        assert perm_action.action == "inject"
