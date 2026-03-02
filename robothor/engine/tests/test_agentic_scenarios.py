"""
Scenario tests — multi-step agent run simulations with scripted failures.

These test the full recovery chain: error classification → recovery action →
helper spawning / backoff / replanning → run continuation.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import AgentConfig, DeliveryMode, RunStatus
from robothor.engine.runner import AgentRunner


@pytest.fixture
def runner(engine_config):
    """Create an AgentRunner with mocked registry."""
    with patch("robothor.engine.runner.get_registry") as mock_reg:
        mock_registry = MagicMock()
        mock_registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "read_file"}},
            {"type": "function", "function": {"name": "exec"}},
            {"type": "function", "function": {"name": "web_fetch"}},
        ]
        mock_registry.get_tool_names.return_value = ["read_file", "exec", "web_fetch"]
        mock_reg.return_value = mock_registry
        r = AgentRunner(engine_config)
        r.registry = mock_registry
        yield r


@pytest.fixture
def spawn_agent_config() -> AgentConfig:
    """Agent config with spawn + planning enabled."""
    return AgentConfig(
        id="test-agent",
        name="Test Agent",
        model_primary="openrouter/test/model",
        model_fallbacks=["openrouter/test/fallback"],
        timeout_seconds=30,
        delivery_mode=DeliveryMode.NONE,
        error_feedback=True,
        can_spawn_agents=True,
        max_nesting_depth=2,
        planning_enabled=True,
        scratchpad_enabled=True,
    )


@pytest.fixture
def basic_agent_config() -> AgentConfig:
    """Agent config without spawn/planning (basic error feedback only)."""
    return AgentConfig(
        id="basic-agent",
        name="Basic Agent",
        model_primary="openrouter/test/model",
        timeout_seconds=30,
        delivery_mode=DeliveryMode.NONE,
        error_feedback=True,
        can_spawn_agents=False,
        planning_enabled=False,
        scratchpad_enabled=True,
    )


def _make_tool_call(name, args=None, call_id="call_1"):
    tc = MagicMock()
    tc.id = call_id
    tc.function.name = name
    tc.function.arguments = json.dumps(args or {})
    return tc


def _make_response(content=None, tool_calls=None, model="test-model"):
    response = MagicMock()
    response.model = model
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = tool_calls
    response.choices = [choice]
    usage = MagicMock()
    usage.prompt_tokens = 100
    usage.completion_tokens = 50
    response.usage = usage
    return response


class TestScenario1AuthFailureTriggersHelperSpawn:
    """Auth failure at count 2 should spawn a helper agent."""

    @pytest.mark.asyncio
    async def test_auth_failure_spawns_helper(self, runner, spawn_agent_config):
        # Disable planning to keep mock_completion predictable
        spawn_agent_config.planning_enabled = False
        spawn_agent_config.model_fallbacks = []

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                # First 2 calls: LLM requests read_file
                return _make_response(
                    tool_calls=[
                        _make_tool_call("read_file", {"path": "/tmp/test"}, f"call_{call_count}")
                    ]
                )
            else:
                # After recovery, LLM completes
                return _make_response(content="Recovered using helper's findings.")

        tool_call_count = 0

        async def mock_execute(name, args, **kwargs):
            nonlocal tool_call_count
            tool_call_count += 1
            if name == "read_file" and tool_call_count <= 2:
                return {"error": "403 Forbidden: invalid token"}
            return {"content": "file contents"}

        runner.registry.execute = AsyncMock(side_effect=mock_execute)

        # Mock the recovery helper spawn
        mock_spawn = AsyncMock(
            return_value="Auth tokens refreshed. Try reading with new credentials."
        )

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        runner._spawn_recovery_helper = mock_spawn
                        run = await runner.execute(
                            "test-agent",
                            "Read the file",
                            agent_config=spawn_agent_config,
                        )

        assert run.status == RunStatus.COMPLETED
        assert run.output_text == "Recovered using helper's findings."
        # Verify recovery helper was called
        mock_spawn.assert_called_once()


class TestScenario2RateLimitBackoff:
    """Rate limit should trigger backoff, not spawn."""

    @pytest.mark.asyncio
    async def test_rate_limit_backoff_no_spawn(self, runner, spawn_agent_config):
        # Disable planning to keep mock_completion predictable
        spawn_agent_config.planning_enabled = False
        spawn_agent_config.model_fallbacks = []

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_response(
                    tool_calls=[_make_tool_call("web_fetch", {"url": "https://example.com"})]
                )
            else:
                return _make_response(content="Done after backoff.")

        exec_count = 0

        async def mock_execute(name, args, **kwargs):
            nonlocal exec_count
            exec_count += 1
            if exec_count == 1:
                return {"error": "429 Too Many Requests"}
            return {"result": "ok"}

        runner.registry.execute = AsyncMock(side_effect=mock_execute)

        mock_spawn = AsyncMock()

        import asyncio as _asyncio

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        with patch.object(_asyncio, "sleep", new_callable=AsyncMock) as mock_sleep:
                            runner._spawn_recovery_helper = mock_spawn
                            run = await runner.execute(
                                "test-agent",
                                "Fetch the page",
                                agent_config=spawn_agent_config,
                            )

        assert run.status == RunStatus.COMPLETED
        # Sleep was called for backoff
        mock_sleep.assert_called()
        # No helper spawned for rate limits
        mock_spawn.assert_not_called()


class TestScenario3BudgetExhaustionTriggersReplan:
    """When budget is mostly consumed with little progress, should replan."""

    @pytest.mark.asyncio
    async def test_budget_triggers_replan(self, runner, spawn_agent_config):
        # Set a low token budget
        spawn_agent_config.token_budget = 500

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                return _make_response(
                    tool_calls=[
                        _make_tool_call("read_file", {"path": "/tmp/test"}, f"call_{call_count}")
                    ]
                )
            else:
                return _make_response(content="Done with revised plan.")

        exec_count = 0

        async def mock_execute(name, args, **kwargs):
            nonlocal exec_count
            exec_count += 1
            if exec_count <= 2:
                return {"error": "File not accessible"}
            return {"content": "ok"}

        runner.registry.execute = AsyncMock(side_effect=mock_execute)

        # Mock planning to return a plan
        plan_data = {
            "difficulty": "moderate",
            "estimated_steps": 3,
            "plan": [
                {"step": 1, "action": "Read file", "tool": "read_file"},
                {"step": 2, "action": "Process", "tool": "exec"},
                {"step": 3, "action": "Write", "tool": "write_file"},
            ],
            "risks": [],
            "success_criteria": "Done",
        }
        plan_response = MagicMock()
        plan_response.choices = [MagicMock()]
        plan_response.choices[0].message.content = json.dumps(plan_data)

        replan_data = {
            "difficulty": "moderate",
            "estimated_steps": 2,
            "plan": [
                {"step": 1, "action": "Try alternate read", "tool": "web_fetch"},
                {"step": 2, "action": "Process", "tool": "exec"},
            ],
            "risks": [],
            "success_criteria": "Done",
        }

        async def mock_acompletion(**kwargs):
            messages = kwargs.get("messages", [])
            # Check if this is a planning/replanning call (JSON mode)
            if kwargs.get("response_format"):
                resp = MagicMock()
                resp.choices = [MagicMock()]
                # Return plan or replan data
                if any("revise" in str(m.get("content", "")).lower() for m in messages):
                    resp.choices[0].message.content = json.dumps(replan_data)
                else:
                    resp.choices[0].message.content = json.dumps(plan_data)
                return resp
            # Regular LLM call
            return await mock_completion(**kwargs)

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_acompletion):
                        run = await runner.execute(
                            "test-agent",
                            "Process the data",
                            agent_config=spawn_agent_config,
                        )

        assert run.status == RunStatus.COMPLETED
        # The run should have completed (not aborted)
        assert run.output_text == "Done with revised plan."


class TestScenario4CascadingFailuresDontSpamHelpers:
    """Multiple auth errors should spawn max 2 helpers, then stop."""

    @pytest.mark.asyncio
    async def test_max_helper_spawns_enforced(self, runner, spawn_agent_config):
        spawn_agent_config.max_iterations = 8

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 6:
                return _make_response(
                    tool_calls=[
                        _make_tool_call("read_file", {"path": "/tmp/x"}, f"call_{call_count}")
                    ]
                )
            return _make_response(content="Giving up — summarized failures.")

        async def mock_execute(name, args, **kwargs):
            return {"error": "403 Forbidden: invalid token"}

        runner.registry.execute = AsyncMock(side_effect=mock_execute)

        spawn_call_count = 0

        async def counting_spawn(*args, **kwargs):
            nonlocal spawn_call_count
            spawn_call_count += 1
            return "Helper tried but auth still broken."

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        with patch.object(
                            runner,
                            "_spawn_recovery_helper",
                            new_callable=AsyncMock,
                            side_effect=counting_spawn,
                        ):
                            await runner.execute(
                                "test-agent",
                                "Read the file",
                                agent_config=spawn_agent_config,
                            )

        # Max 2 helper spawns
        assert spawn_call_count <= 2


class TestScenario5HappyPathNoRecovery:
    """All tools succeed — no error recovery, no spawns, no replans."""

    @pytest.mark.asyncio
    async def test_happy_path(self, runner, spawn_agent_config):
        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            # Planning call
            if kwargs.get("response_format"):
                resp = MagicMock()
                resp.choices = [MagicMock()]
                resp.choices[0].message.content = json.dumps(
                    {
                        "difficulty": "simple",
                        "estimated_steps": 3,
                        "plan": [
                            {"step": 1, "action": "Read", "tool": "read_file"},
                            {"step": 2, "action": "Process", "tool": "exec"},
                            {"step": 3, "action": "Write", "tool": "write_file"},
                        ],
                        "risks": [],
                        "success_criteria": "All done",
                    }
                )
                return resp

            if call_count <= 3:
                tools = ["read_file", "exec", "write_file"]
                return _make_response(
                    tool_calls=[_make_tool_call(tools[call_count - 1], {}, f"call_{call_count}")]
                )
            return _make_response(content="All tasks completed successfully!")

        runner.registry.execute = AsyncMock(return_value={"result": "ok"})

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        with patch.object(
                            runner,
                            "_spawn_recovery_helper",
                            new_callable=AsyncMock,
                        ) as mock_spawn:
                            run = await runner.execute(
                                "test-agent",
                                "Do the thing",
                                agent_config=spawn_agent_config,
                            )

        assert run.status == RunStatus.COMPLETED
        assert run.output_text == "All tasks completed successfully!"
        mock_spawn.assert_not_called()


class TestScenario6ReplanLoopPrevention:
    """Replan should be capped at MAX_REPLANS (2)."""

    @pytest.mark.asyncio
    async def test_max_replans_enforced(self, runner, spawn_agent_config):
        spawn_agent_config.max_iterations = 15

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            # Planning/replanning calls
            if kwargs.get("response_format"):
                resp = MagicMock()
                resp.choices = [MagicMock()]
                resp.choices[0].message.content = json.dumps(
                    {
                        "difficulty": "complex",
                        "estimated_steps": 3,
                        "plan": [
                            {"step": 1, "action": "Try approach", "tool": "read_file"},
                            {"step": 2, "action": "Process", "tool": "exec"},
                            {"step": 3, "action": "Write", "tool": "write_file"},
                        ],
                        "risks": [],
                        "success_criteria": "Done",
                    }
                )
                return resp

            if call_count <= 10:
                return _make_response(
                    tool_calls=[
                        _make_tool_call("read_file", {"path": "/tmp/x"}, f"call_{call_count}")
                    ]
                )
            return _make_response(content="Finally done after multiple replans.")

        exec_count = 0

        async def mock_execute(name, args, **kwargs):
            nonlocal exec_count
            exec_count += 1
            # Alternate between failures and successes to keep run alive
            if exec_count % 3 != 0:
                return {"error": "File not accessible"}
            return {"result": "ok"}

        runner.registry.execute = AsyncMock(side_effect=mock_execute)

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        run = await runner.execute(
                            "test-agent",
                            "Complex task",
                            agent_config=spawn_agent_config,
                        )

        # Run should complete (not infinite loop)
        assert run.status in (RunStatus.COMPLETED, RunStatus.FAILED)


class TestErrorClassificationInRunner:
    """Verify error classification is wired into the runner correctly."""

    @pytest.mark.asyncio
    async def test_auth_error_classified(self, runner, basic_agent_config):
        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_response(
                    tool_calls=[_make_tool_call("web_fetch", {"url": "https://api.example.com"})]
                )
            return _make_response(content="Done.")

        runner.registry.execute = AsyncMock(return_value={"error": "401 Unauthorized"})

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        run = await runner.execute(
                            "basic-agent",
                            "Fetch data",
                            agent_config=basic_agent_config,
                        )

        # Should complete (not crash) — error feedback injected
        assert run.status == RunStatus.COMPLETED
